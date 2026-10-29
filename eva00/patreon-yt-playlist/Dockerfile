FROM python:3.13-alpine
# curl_cffi: Chrome TLS fingerprint for the Patreon login endpoint (Cloudflare blocks urllib there)
RUN pip install --no-cache-dir curl_cffi==0.17.0
COPY app.py /app/app.py
CMD ["python", "-u", "/app/app.py"]
