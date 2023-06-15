ARG source
ARG source_commit_sha
FROM $source AS builder
LABEL maintainer="lambshm developers <autumn.jolitz+lambshm@gmail.com>"

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

FROM $source
ENV AWS_LAMBDA_EXEC_WRAPPER='/opt/lambshm/bin/bootstrap'
COPY --from=builder /usr/obj/root /