FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py serve_http.py cache.py english.py revision_diff.py xref.py ./

ENV PORT=8000
EXPOSE 8000

# Requires LAW_MCP_TOKEN=<random string> or LAW_MCP_PUBLIC=1 at `docker run`
# time (see README "Hosted / HTTP deployment") — serve_http.py refuses to
# start with neither set.
CMD ["python", "serve_http.py"]
