FROM rasa/rasa:3.6.21
WORKDIR /app
COPY --chown=1001:1001 . /app

# Train the small Spanish starter assistant during the image build.
RUN rasa train

EXPOSE 10000

# Rasa's REST API is protected with a secret configured in Render.
# Render supplies PORT; 10000 is the local/default fallback.
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["test -n \"$RASA_AUTH_TOKEN\" || { echo 'RASA_AUTH_TOKEN is required'; exit 1; }; exec rasa run --enable-api -i 0.0.0.0 -p \"${PORT:-10000}\" --credentials credentials.yml -t \"$RASA_AUTH_TOKEN\""]
