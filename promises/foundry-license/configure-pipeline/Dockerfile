FROM alpine:3.19

RUN apk add --no-cache bash curl jq yq && \
    curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    chmod +x kubectl && \
    mv kubectl /usr/local/bin/

COPY scripts/ /scripts/

RUN chmod +x /scripts/*.sh

WORKDIR /kratix
