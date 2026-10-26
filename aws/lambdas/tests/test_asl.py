import json
import re
from pathlib import Path

AWS = Path(__file__).resolve().parents[2]
PLACEHOLDERS = {"IngestFnArn", "ExtractDocFnArn", "PlanAudioFnArn", "TranscribeChunkFnArn", "AnalyzeImageFnArn", "AssembleFnArn"}


def load():
    return json.loads((AWS / "statemachine.asl.json").read_text())


def walk_machines(m):
    """The top-level machine, every Map's item processor and every Parallel branch."""
    yield m
    for st in m["States"].values():
        if st["Type"] == "Map":
            yield from walk_machines(st["ItemProcessor"])
        elif st["Type"] == "Parallel":
            for branch in st["Branches"]:
                yield from walk_machines(branch)


def targets(st):
    out = [st["Next"]] if "Next" in st else []
    out += [c["Next"] for c in st.get("Catch", [])]
    out += [c["Next"] for c in st.get("Choices", [])]
    if "Default" in st:
        out.append(st["Default"])
    return out


def file_states():
    return load()["States"]["ProcessFiles"]["ItemProcessor"]["States"]


def video_branches():
    audio, visual = file_states()["AnalyzeVideo"]["Branches"]
    return audio["States"], visual["States"]


def test_asl_is_consistent():
    asl = load()
    for m in walk_machines(asl):
        names = set(m["States"])
        assert m["StartAt"] in names
        reached, todo = set(), [m["StartAt"]]
        while todo:
            n = todo.pop()
            if n in reached:
                continue
            reached.add(n)
            for t in targets(m["States"][n]):
                assert t in names, f"{n} -> {t} missing"
                todo.append(t)
        assert reached == names, f"unreachable: {names - reached}"
        for n, st in m["States"].items():
            if st["Type"] not in ("Succeed", "Fail", "Choice"):
                assert "Next" in st or st.get("End"), n


def test_placeholders_and_concurrency():
    text = (AWS / "statemachine.asl.json").read_text()
    assert set(re.findall(r"\$\{(\w+)\}", text)) == PLACEHOLDERS
    asl = load()
    # Deliberately small: a new account only gets 10 concurrent Lambdas, shared by every evaluation.
    assert asl["States"]["ProcessFiles"]["MaxConcurrency"] == 2
    audio, visual = video_branches()
    assert file_states()["AnalyzeImages"]["MaxConcurrency"] == 3 and audio["TranscribeChunks"]["MaxConcurrency"] == 3
    assert visual["AnalyzeSheets"]["MaxConcurrency"] == 3


def test_video_runs_audio_and_frames_in_parallel_then_merges():
    inner = file_states()
    video = next(c for c in inner["RouteFile"]["Choices"] if c["StringEquals"] == "video")
    assert video["Next"] == "AnalyzeVideo" and inner["AnalyzeVideo"]["Type"] == "Parallel"
    assert inner["AnalyzeVideo"]["Next"] == "MergeVideo" and inner["AnalyzeVideo"]["ResultPath"] is None
    # even a Parallel failure still merges whatever was produced
    assert inner["AnalyzeVideo"]["Catch"][0]["Next"] == "MergeVideo"
    assert inner["MergeVideo"]["Parameters"]["Payload"]["mode"] == "merge_video"
    audio, visual = video_branches()
    assert audio["PlanAudio"]["Next"] == "TranscribeChunks"
    assert visual["PlanFrames"]["Next"] == "AnalyzeSheets"
    # PlanFrames runs on the plan-audio function (10 GB /tmp); AnalyzeSheets on the analyze-image function
    assert visual["PlanFrames"]["Parameters"]["FunctionName"] == "${PlanAudioFnArn}"
    assert visual["PlanFrames"]["Parameters"]["Payload"]["mode"] == "plan_frames"
    sheet = visual["AnalyzeSheets"]["ItemProcessor"]["States"]["AnalyzeSheet"]
    assert sheet["Parameters"]["FunctionName"] == "${AnalyzeImageFnArn}"
    assert visual["AnalyzeSheets"]["ItemSelector"]["mode"] == "video_sheets"
    assert visual["AnalyzeSheets"]["ItemsPath"] == "$.frames.batches"
    assert any("TransientError" in r["ErrorEquals"] for r in sheet["Retry"])


def test_a_branch_failure_never_ends_the_file():
    """No audio, a crashed PlanAudio or a failed visual analysis only ends its own branch: MergeVideo still runs
    (a visual failure becomes a warning, a silent video still gets its frame notes)."""
    audio, visual = video_branches()
    for branch, gave_up in ((audio, "AudioGaveUp"), (visual, "VisualGaveUp")):
        assert branch[gave_up] == {"Type": "Pass", "End": True}
        for name, st in branch.items():
            if st["Type"] in ("Task", "Map"):
                assert [c["Next"] for c in st["Catch"]] == [gave_up], name
    item = visual["AnalyzeSheets"]["ItemProcessor"]["States"]
    assert item["AnalyzeSheet"]["Catch"][0]["Next"] == "SheetGaveUp" and item["SheetGaveUp"]["Type"] == "Pass"


def test_every_lambda_task_retries_service_errors():
    for m in walk_machines(load()):
        for n, st in m["States"].items():
            if st["Type"] == "Task" and n != "HandleFailure":
                errs = {e for r in st["Retry"] for e in r["ErrorEquals"]}
                assert "Lambda.TooManyRequestsException" in errs and "Lambda.ServiceException" in errs, n
                for r in st["Retry"]:
                    if "Lambda.TooManyRequestsException" in r["ErrorEquals"]:
                        # Throttling means "wait your turn", not "fail": many patient, jittered, capped retries.
                        assert r["MaxAttempts"] >= 20 and r.get("JitterStrategy") == "FULL", n
                        assert r.get("MaxDelaySeconds", 0) >= 20 and r.get("BackoffRate", 1) >= 1.3, n
                    else:
                        assert r.get("BackoffRate", 1) >= 2, n


def test_failure_path_writes_status_then_fails():
    asl = load()["States"]
    for n in ("Ingest", "ProcessFiles", "Assemble"):
        assert asl[n]["Catch"][0]["Next"] == "HandleFailure"
    assert asl["HandleFailure"]["Parameters"]["Payload"]["mode"] == "fail"
    assert asl["PipelineFailed"]["Type"] == "Fail"


def test_policy_templates_are_valid_json():
    for p in (AWS / "policies").glob("*.json"):
        json.loads(p.read_text())
