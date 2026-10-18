# The bridge image plus what the tests need, so tests use the ffmpeg and PyChromecast that deploy.
#   docker build -q -t cast-bridge:dev cast-bridge
#   docker build -q -t cast-bridge:test cast-bridge/tests
#   docker run --rm --network host -v "$PWD/cast-bridge:/src:ro" --entrypoint python cast-bridge:test /src/tests/<test>.py
ARG BRIDGE_IMAGE=cast-bridge:dev
FROM ${BRIDGE_IMAGE}
RUN pip install --no-cache-dir numpy==2.3.4
