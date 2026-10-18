#!/usr/bin/python3
"""Minimal PostgreSQL initialization contract for pg_stand's custom maker image."""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*argv):
    subprocess.run(argv, check=True)


def postgres(*argv):
    run("gosu", "postgres", *argv)


def main():
    args = sys.argv[1:] or ["postgres"]
    if args[0] != "postgres":
        os.execvp(args[0], args)
    data = Path(os.environ["PGDATA"])
    user = os.environ.get("POSTGRES_USER", "postgres")
    database = os.environ.get("POSTGRES_DB", user)
    init_args = shlex.split(os.environ.get("POSTGRES_INITDB_ARGS", ""))
    for directory in (str(data), "/pgwal", "/pglog"):
        run("install", "-d", "-m", "0700", "-o", "postgres", "-g", "postgres", directory)
    run("install", "-d", "-m", "2775", "-o", "postgres", "-g", "postgres", "/run/postgresql")
    if not (data / "PG_VERSION").is_file():
        password = os.environ.get("POSTGRES_PASSWORD", "")
        if not password or "\n" in password or "\r" in password:
            raise RuntimeError("POSTGRES_PASSWORD must be non-empty and single-line")
        fd, filename = tempfile.mkstemp(prefix="pg-maker-pw-", dir="/tmp")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(password + "\n")
            run("chown", "postgres:postgres", filename)
            postgres(
                "initdb",
                "-D",
                str(data),
                "--username=" + user,
                "--pwfile=" + filename,
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                *init_args,
            )
        finally:
            Path(filename).unlink(missing_ok=True)
        postgres(
            "pg_ctl",
            "-D",
            str(data),
            "-w",
            "-o",
            "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql",
            "start",
        )
        try:
            if database != "postgres":
                postgres(
                    "createdb",
                    "-h",
                    "/var/run/postgresql",
                    "-U",
                    user,
                    "--owner=" + user,
                    "--",
                    database,
                )
            auth = Path("/docker-entrypoint-initdb.d/pg-stand-auth.sh")
            if auth.is_file():
                postgres("sh", str(auth))
        finally:
            postgres("pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop")
    os.environ.pop("POSTGRES_PASSWORD", None)
    os.execvp(
        "gosu", ["gosu", "postgres", *args, "-c", "unix_socket_directories=/var/run/postgresql"]
    )


if __name__ == "__main__":
    main()
