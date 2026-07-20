FROM alpine:3.22

RUN apk add --no-cache autossh openssh-client \
    && adduser -D -H -u 1026 -G users tunnel

ENTRYPOINT ["autossh"]
