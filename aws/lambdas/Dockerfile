# One container image for all six qs-or (OpenRouter variant) Lambda functions. Each function selects its handler through the
# Lambda "ImageConfig.Command" override (worker.handlers.ingest, ...extract_doc, ...plan_audio, ...transcribe_chunk,
# ...analyze_image, ...assemble). Build for x86_64 with `--provenance=false` so Lambda accepts the manifest.
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
# imageio-ffmpeg wheels bundle a static ffmpeg binary (no system packages needed on the Lambda base image).
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt \
 && python -c "import imageio_ffmpeg, subprocess; p = imageio_ffmpeg.get_ffmpeg_exe(); print(p); subprocess.run([p, '-version'], check=True)" \
 && ln -sf "$(python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" /usr/local/bin/ffmpeg

COPY src/worker ${LAMBDA_TASK_ROOT}/worker

ENV FFMPEG_BIN=/usr/local/bin/ffmpeg \
    PYTHONUNBUFFERED=1

CMD ["worker.handlers.ingest"]
