FROM python:3.12-alpine

ENV LANG=C.UTF-8

RUN apk add --no-cache jq tzdata

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY heiko_predictive_control/ ./heiko_predictive_control/
COPY run.sh /
RUN chmod +x /run.sh

CMD ["/run.sh"]
