"""Production entry point for the SSJ Waitress service."""

import os

from apps.core.paths import deployment_env_path, load_runtime_environment


# The production runner intentionally prefers the Controller-managed file.
managed_env = deployment_env_path()
if managed_env.is_file():
    os.environ['SSJ_ENV_FILE'] = str(managed_env)
load_runtime_environment()

from waitress import serve  # noqa: E402
from run import create_app  # noqa: E402


def main() -> None:
    load_runtime_environment()
    app = create_app()
    host = os.environ.get('SSJ_INTERNAL_HOST') or app.config.get('SSJ_INTERNAL_HOST', '0.0.0.0')
    port = int(os.environ.get('SSJ_INTERNAL_PORT') or app.config.get('SSJ_INTERNAL_PORT', 32425))
    serve(
        app,
        host=host,
        port=port,
        threads=8,
    )


if __name__ == '__main__':
    main()
