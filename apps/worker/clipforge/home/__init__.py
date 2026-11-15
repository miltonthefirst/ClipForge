"""The home wifi route: a phone at home plays clips straight off this PC.

Run by the machine agent (clipforge/agent.py), switched on by an admin from the
app, and drawn from the shared home-route blueprint (P:/Experiments/blueprints).
See docs/HOME-NETWORK-PLAN.md.

Nothing here is imported unless the route is wanted: the listener needs the
`home` extra (FastAPI and uvicorn), which a machine that never switches the
route on need not install.
"""
