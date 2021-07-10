FROM python:3.6

USER default

# Create app directory
WORKDIR /app

# Install app dependencies
COPY --chown=default . .

RUN mkdir /app/analytics && \
    mkdir /app/cookies && \
    mkdir /app/logs && \
    chown -R default:root /app

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

CMD [ "python", "run.py" ]