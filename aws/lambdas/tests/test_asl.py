import json
import re
from pathlib import Path

AWS = Path(__file__).resolve().parents[2]
PLACEHOLDERS = {"IngestFnArn", "ExtractDocFnArn", "PlanAudioFnArn", "TranscribeChunkFnArn", "AnalyzeImageFnArn", "AssembleFnArn"}


def load():
    return json.loads((AWS / "statemachine.asl.json").read_text())


def walk_machines(m):
    yield m
    for st in m["States"].values():
        if st["Type"] == "Map":
            yield from walk_machines(st["ItemProcessor"])


def targets(st):
    out = [st["Next"]] if "Next" in st else []
    out += [c["Next"] for c in st.get("Catch", [])]
    out += [c["Next"] for c in st.get("Choices", [])]
    if "Default" in st:
        out.append(st["Default"])
    return out


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
    inner = asl["States"]["ProcessFiles"]["ItemProcessor"]["States"]
    assert inner["AnalyzeImages"]["MaxConcurrency"] == 3 and inner["TranscribeChunks"]["MaxConcurrency"] == 3


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
