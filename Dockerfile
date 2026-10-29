FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY physics_witness ./physics_witness
COPY scripts ./scripts
RUN pip install --no-cache-dir .

# Sample claims ("No video? Try a sample"): the list, its videos and the price table.
# demo/videos/ is git-ignored but not docker-ignored, so clips you keep only on your
# machine (e.g. demo/videos/dallas.mp4) are baked into the image you build there.
COPY demo ./demo
COPY short_test_video.webm pricing*.json ./
ENV PW_SAMPLES=/app/demo/samples.json

# ffmpeg ships inside the imageio-ffmpeg wheel, so no apt packages are needed.
ENV PW_CACHE_DIR=/data/cache
VOLUME ["/data"]
EXPOSE 8000
ENTRYPOINT ["physics-witness"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000", "--data", "/data/claims"]
