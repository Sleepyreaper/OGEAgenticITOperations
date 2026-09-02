"""Testable Azure signal collectors for the operations evidence layer.

Every collector function in this package takes its Azure clients
(credential factory, HTTP getter, Log Analytics query function) as
injectable parameters with real defaults, so tests can pass fakes and
never make a live network/Azure call. See app/operations/collectors/http.py
for the shared ARM REST helper used by the alerts/capacity collectors.
"""
