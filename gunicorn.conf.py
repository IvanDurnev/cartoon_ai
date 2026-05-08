import multiprocessing
import os


bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8001")

# This app starts background queue workers inside the web process, so we keep
# a single Gunicorn worker process to avoid duplicating those workers.
workers = int(os.getenv("GUNICORN_WORKERS", "1"))
worker_class = "gthread"
threads = int(
    os.getenv(
        "GUNICORN_THREADS",
        str(min(8, max(4, multiprocessing.cpu_count() * 2))),
    )
)

timeout = int(os.getenv("GUNICORN_TIMEOUT", "300"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")

capture_output = True
enable_stdio_inheritance = True

preload_app = False

