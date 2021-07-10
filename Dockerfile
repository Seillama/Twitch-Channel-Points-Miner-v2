FROM python:3.6

# Create app directory
WORKDIR /app

# Install app dependencies
COPY . .

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

CMD [ "python", "run.py" ]