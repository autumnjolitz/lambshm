ARG BASE_IMAGE_DIGEST
FROM $BASE_IMAGE_DIGEST AS builder

ADD requirements.txt .
RUN python -m pip install -r requirements.txt && yum install -y gcc binutils

COPY bin /opt/lambshm/bin
ENV SHM_FOLDER_PREFIX='/tmp/shm/' \
    PATH="$PATH:/opt/lambshm/bin"

RUN patch-libc.py --shm-folder $SHM_FOLDER_PREFIX --copy-to /usr/obj/root && \
    mkdir -p /usr/obj/root/opt/lambshm/bin && \
    export PATTERN="s;\"\${SHM_FOLDER_PREFIX\:\-}\";'"$(echo $SHM_FOLDER_PREFIX | sed 's;/;\\/;g')"';g" && \
    echo sed $PATTERN /opt/lambshm/bin/bootstrap && \
    sed "${PATTERN}" /opt/lambshm/bin/bootstrap > /usr/obj/root/opt/lambshm/bin/bootstrap && \
    chmod u+rwx,g+rx,o+rx /usr/obj/root/opt/lambshm/bin/*

FROM $BASE_IMAGE_DIGEST
ARG TODAY
ARG BASE_IMAGE
ARG BASE_IMAGE_DIGEST

ENV AWS_LAMBDA_EXEC_WRAPPER='/opt/lambshm/bin/bootstrap'
COPY --from=builder /usr/obj/root /

LABEL org.opencontainers.image.authors="lambshm developers <autumn.jolitz+lambshm@gmail.com>" \
      org.opencontainers.image.source="https://github.com/autumnjolitz/lambshm" \
      org.opencontainers.image.title="LambSHM" \
      org.opencontainers.image.description="Patched libc for common Python images used in Lambda" \
      org.opencontainers.image.created=$TODAY \
      org.opencontainers.image.base.digest=$BASE_IMAGE_DIGEST \
      org.opencontainers.image.base.name=$BASE_IMAGE