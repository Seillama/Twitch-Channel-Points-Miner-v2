FROM python:3.6

# Install app dependencies
COPY --chown=default . /app

RUN pip install -r /app/requirements.txt --no-cache-dir

RUN chgrp -R 0 /app && \
    chmod -R g=u /app

USER  1001

CMD [ "python", "/app/run.py" ]