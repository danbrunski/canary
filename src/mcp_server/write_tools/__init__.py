"""Write tools — proxy daemon control endpoints, audit every call.

Every function in this subpackage POSTs to a ``/control/...`` route. The
audit log entry is written *before* returning so the trail captures the
attempted call even on failure (the ``result`` field records ``"ok"`` or
the failure code).
"""
