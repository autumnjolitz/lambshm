=============
LambShm
=============

|Docker Image CI|


Python 3.8 and above use ``shm_open(2)`` to manage threading and multiprocess objects. With the looming deprecation of Python 3.7 on AWS Lambda, it's become even harder to use common Python threading and multiprocessing patterns [#f1]_ [#f2]_:

This wouldn't be a problem if Amazon mounted ``tmpfs /dev/shm tmpfs rw,nosuid,size=65536k,mode=755 0 0`` (in ``/etc/fstab``), however *they have not done that for years* and won't budge.

Only ``/tmp`` is write accessible for any real-world Lambda invocation.


So Python 3.8+ developers will see:

.. code-block:: bash
    :name: broken-example

    InvincibleReason:~/software/lambshm [main]$ \
    >         docker run --rm --entrypoint /var/lang/bin/python \
    >         --ipc=none --tmpfs /tmp --read-only --user 1000 \
    >         -it public.ecr.aws/lambda/python:3.8 \
    >         -c 'from multiprocessing import Queue; print(Queue())'
    Traceback (most recent call last):
      File "<string>", line 1, in <module>
      File "/var/lang/lib/python3.8/multiprocessing/context.py", line 103, in Queue
        return Queue(maxsize, ctx=self.get_context())
      File "/var/lang/lib/python3.8/multiprocessing/queues.py", line 42, in __init__
        self._rlock = ctx.Lock()
      File "/var/lang/lib/python3.8/multiprocessing/context.py", line 68, in Lock
        return Lock(ctx=self.get_context())
      File "/var/lang/lib/python3.8/multiprocessing/synchronize.py", line 162, in __init__
        SemLock.__init__(self, SEMAPHORE, 1, 1, ctx=ctx)
      File "/var/lang/lib/python3.8/multiprocessing/synchronize.py", line 57, in __init__
        sl = self._semlock = _multiprocessing.SemLock(
    PermissionError: [Errno 13] Permission denied
    InvincibleReason:~/software/lambshm [main]$

☹️ Oh no!

Let's give **lambshm/python3.8** a try!

Since we can only write to ``/tmp``, I binary patched **glibc-2.27** to write posix shared memory on ``/tmp/shm/`` (`Shared Memory Folder`_):

.. code-block:: bash
    :name: working-example

    InvincibleReason:~/software/lambshm [main]$ \
    >         docker run --rm --entrypoint /var/lang/bin/python \
    >         --ipc=none --tmpfs /tmp --read-only --user 1000 \
    >         -it ghcr.io/autumnjolitz/lambshm/python3.8:latest \
    >         -c 'from multiprocessing import Queue; print(Queue())'
    <multiprocessing.queues.Queue object at 0x7fe47e2954c0>
    InvincibleReason:~/software/lambshm [main]$

Quickstart
--------------

.. code-block:: Dockerfile
    :name: sample-dockerfile

    FROM ghcr.io/autumnjolitz/lambshm/python3.8:latest

    ADD lambda_handler.py .
    CMD ["lambda_handler.handler"]



Build and run test
---------------------

.. code-block:: bash
    :name: build-example

    docker compose --ansi never \
        -f config/docker-compose.yml \
        build --progress plain \
        lambda_py38 && \
    docker compose --ansi never  \
        -f config/docker-compose.yml -f config/docker-compose.test.yml \
        build --progress plain \
        lambda_py38 && \
    docker compose --ansi never  -f config/docker-compose.yml -f config/docker-compose.test.yml run --entrypoint /bin/sh --rm lambda_py38 -c 'mkdir /tmp/shm && python /var/task/lambda_handler.py'



At terminal 1:

.. code-block:: bash
    :name: start-test-server

    (python) InvincibleReason:~/software/lambshm$ \
    >     docker compose --ansi never  -f docker-compose.yml build --progress plain lambda_py38 && \
    >     docker compose --ansi never  -f docker-compose.yml -f docker-compose.test.yml build \
    >         --progress plain lambda_py38 && \
    >     docker compose --ansi never  -f docker-compose.yml -f docker-compose.test.yml run --service-ports --rm lambda_py38
    #1 [internal] load build definition from Dockerfile
    #1 transferring dockerfile: 495B done
    #1 DONE 0.0s

    ... /snip
    #6 [builder 2/6] ADD requirements.txt .
    #6 CACHED

    #7 [builder 3/6] RUN python -m pip install -r requirements.txt
    #7 1.031 Collecting Mako==1.2.4
    /snip
    #9 exporting to image
    #9 exporting layers 0.0s done
    #9 writing image sha256:82ee987e4dd3eab8d8108a8d4b5dac6d9ef5facde6e327dc5f6543d7864ee501 done
    #9 naming to docker.io/library/lambshm/python3.8-test done
    #9 DONE 0.1s

    Use 'docker scan' to run Snyk tests against images to find vulnerabilities and learn how to fix them
    Network lambshm_default  Creating
    Network lambshm_default  Created
    15 Jun 2023 17:59:47,794 [INFO] (rapid) exec '/var/runtime/bootstrap' (cwd=/var/task, handler=)
    15 Jun 2023 17:59:49,406 [INFO] (rapid) extensionsDisabledByLayer(/opt/disable-extensions-jwigqn8j) -> stat /opt/disable-extensions-jwigqn8j: no such file or directory


Switch to terminal 2:

.. code-block:: bash
    :name: run-handler

    InvincibleReason:~$ curl \
        -s -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
        -d '{"limit": 21}' | jq
    {
      "by_return": [
        0,
        1,
        2,
        4,
        6,
        125,
        1296,
        12,
        262144,
        16,
        100000000,
        2357947691,
        22,
        1792160394037,
        56693912375296,
        28,
        30,
        32,
        121439531096594250000,
        36,
        38
      ],
      "by_queue": [
        0,
        6,
        100000000,
        2,
        16,
        1,
        12,
        125,
        56693912375296,
        1792160394037,
        30,
        22,
        1296,
        28,
        121439531096594250000,
        36,
        38,
        32,
        262144,
        2357947691,
        4
      ]
    }
    InvincibleReason:~$

Back to terminal 1:

.. code-block:: bash
    :name: stop-test-server

    START RequestId: da77a8ed-eb03-4ec3-b005-62d441f94de2 Version: $LATEST
    15 Jun 2023 17:59:49,407 [INFO] (rapid) Configuring and starting Operator Domain
    15 Jun 2023 17:59:49,407 [INFO] (rapid) Starting runtime domain
    15 Jun 2023 17:59:49,407 [WARNING] (rapid) Cannot list external agents error=open /opt/extensions: no such file or directory
    15 Jun 2023 17:59:49,407 [INFO] (rapid) Starting runtime without AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN , Expected?: false
    All shared memory files will open with a filename prefix /tmp/shm/
    END RequestId: f3f6cabd-6565-4340-a2c6-070adfde9ecd
    REPORT RequestId: f3f6cabd-6565-4340-a2c6-070adfde9ecd  Init Duration: 0.29 ms  Duration: 210.99 ms Billed Duration: 211 ms Memory Size: 3008 MB    Max Memory Used: 3008 MB
    START RequestId: 2a87e068-0044-45f6-a053-383fd4d3610a Version: $LATEST
    END RequestId: 8a88c4be-d23a-4e4a-8936-de949a3205e0
    REPORT RequestId: 8a88c4be-d23a-4e4a-8936-de949a3205e0  Duration: 35.88 ms  Billed Duration: 36 ms  Memory Size: 3008 MB    Max Memory Used: 3008 MB
    ^C15 Jun 2023 18:00:07,312 [INFO] (rapid) Received signal signal=interrupt
    15 Jun 2023 18:00:07,312 [INFO] (rapid) Shutting down...
    15 Jun 2023 18:00:07,313 [WARNING] (rapid) Reset initiated: SandboxTerminated
    15 Jun 2023 18:00:07,313 [INFO] (rapid) Sending SIGKILL to runtime-1(16).
    15 Jun 2023 18:00:07,318 [INFO] (rapid) Stopping runtime domain
    15 Jun 2023 18:00:07,319 [INFO] (rapid) Waiting for runtime domain processes termination
    15 Jun 2023 18:00:07,319 [INFO] (rapid) Stopping operator domain
    15 Jun 2023 18:00:07,319 [INFO] (rapid) Starting runtime domain
    (python) InvincibleReason:~/software/lambshm [main]$


Notes
^^^^^^^

Running as sbx_user1005
*****************************

Lambda runs the actual processes with a restricted user (``sbx_user1005``), no ``/dev/shm`` (``--ipc=none``) [#f3]_, read-only file system (``--read-only``) with only ``/tmp`` left writeable (``--tmpfs /tmp``). However all docker builds take place with the ``root`` user and are left as ``root`` in order to allow additional build configurations.

The test image is configured with ``docker compose`` to run as close to the same configuration as Amazon Lambda does.

Shared Memory Folder
*********************

Current chosen prefix for files is ``/tmp/shm/`` to avoid any collisions. There's a ``AWS_LAMBDA_EXEC_WRAPPER`` specified to create the missing directory at run-time for a lambda server instance.


.. [#f1] https://aws.amazon.com/blogs/compute/parallel-processing-in-python-with-aws-lambda/
.. [#f2] https://medium.com/tech-carnot/understanding-multiprocessing-in-aws-lambda-with-python-6f50c11d57e4
.. [#f3] https://github.com/lambci/docker-lambda/issues/26
.. |Docker Image CI| image:: https://github.com/autumnjolitz/lambshm/actions/workflows/docker-image.yml/badge.svg
   :target: https://github.com/autumnjolitz/lambshm/actions/workflows/docker-image.yml
