FROM rasa/rasa:3.6.21
WORKDIR /app
COPY --chown=1001:1001 . /app

# Verify Firebase ID tokens at the public gateway; Rasa remains loopback-only.
RUN python -m pip install --no-cache-dir -r requirements-gateway.txt

# Train the small Spanish starter assistant during the image build.
RUN rasa train

EXPOSE 10000
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["test -n \"$RASA_AUTH_TOKEN\" || { echo 'RASA_AUTH_TOKEN is required'; exit 1; }; exec python secure_gateway.py"]
