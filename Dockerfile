FROM python:3.11-slim

# Google's official Google Ads MCP server, pinned to a tested commit.
# To update: change this to a newer commit from
# https://github.com/googleads/google-ads-mcp/commits/main and test again.
ARG GOOGLE_ADS_MCP_COMMIT=0b78c0caa6d1dfbd21817487c7e571514c627c87
ARG GOOGLE_ADS_MCP_SOURCE=https://github.com/googleads/google-ads-mcp/archive/${GOOGLE_ADS_MCP_COMMIT}.tar.gz

WORKDIR /app
COPY constraints.txt .
RUN pip install --no-cache-dir --constraint constraints.txt \
    "google-ads-mcp[firestore] @ ${GOOGLE_ADS_MCP_SOURCE}"

COPY *.py ./

# Cloud Run sends traffic to port 8080.
EXPOSE 8080
USER nobody
CMD ["python", "server.py"]
