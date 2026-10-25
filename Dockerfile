FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 10001 translate && mkdir /data && chown translate /data
USER translate
ENV TRANSLATION_HOST=0.0.0.0 TRANSLATION_DATABASE=/data/translations.sqlite3
EXPOSE 18448
VOLUME /data
CMD ["mc-translate"]
