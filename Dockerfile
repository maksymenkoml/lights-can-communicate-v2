# The core image: the pinned diyHue runtime (the gateway's Hue implementation and
# Python) with the core's archive and gateway modules. See
# docs/design/2026-11-26-container-image.md.
ARG BASE=diyhue/core@sha256:392c1c39a5b46168d1d6d8582abd266586bdd45d61fb3459fa95821c710ffcba

FROM ${BASE} AS build
WORKDIR /src
COPY . .
RUN python3 -B -m lights_can_communicate_v2_core.dist --output /release

FROM ${BASE}
COPY --from=build /release/gateway/ /probe/
# The gateway imports the core package from the archive next to its modules; the
# entrypoint runs the same archive.
COPY --from=build /release/lights-can-communicate-v2.pyz /probe/lights-can-communicate-v2.pyz
WORKDIR /data
VOLUME /data
STOPSIGNAL SIGTERM
LABEL org.opencontainers.image.source="https://github.com/maksymenkoml/lights-can-communicate-v2" \
      org.opencontainers.image.description="Lights Can Communicate v2 core: virtual Hue bridge, Govee adapter and Light Studio"
ENTRYPOINT ["python3", "-I", "-B", "/probe/lights-can-communicate-v2.pyz", "container"]
CMD []
