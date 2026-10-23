"""The run model trackinizer shares with the agent runner: asks, redaction, adapters.

The agent runner is its own package, and imports this one.
What the server and other readers of a run's records also need -- the records an ask
writes, the secrets a transcript must not carry, the readers of each CLI's session
log -- stays here, so none of them depends on the endpoints.
"""
