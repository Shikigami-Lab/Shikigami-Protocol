"""Keep container writes owned by the source application's filesystem user."""
import os


def container_identity():
    # Windows bind mounts use Docker Desktop's file sharing; retain the image default.
    getuid, getgid = getattr(os, "getuid", None), getattr(os, "getgid", None)
    return (getuid(), getgid()) if callable(getuid) and callable(getgid) else (1000, 1000)
