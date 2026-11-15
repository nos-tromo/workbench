"""The data volume's layout: the names of its parts, shared by the api and the executor.

``workbench.executor.prepare_volume`` lays them out and gives each its owner and mode; the api's store, queue and
retention and the executor's supervisor find them by these names. Names only: the api imports nothing of the executor.
"""

from typing import Final

#: The users' files, ``workspaces/<caller>/<user-id>/<chat-id>/…``; the api's.
WORKSPACES: Final = "workspaces"

#: The outputs the executor hands over, ``staging/<job-id>/<name>``; the api's.
STAGING: Final = "staging"

#: One private folder per running job; root's.
JOBS: Final = "jobs"

#: The api's database, in the volume's root.
DATABASE: Final = "workbench.db"
