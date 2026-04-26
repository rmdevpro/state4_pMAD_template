"""Peer proxy — stub for inter-MAD communication (ERQ-002 §13.2).

Provides TEs with a client for reaching other MADs in the ecosystem.
Not yet implemented; returns None until the context broker integration is wired up.
"""


def get_peer_proxy():
    """Return a peer proxy client, or None if not configured."""
    return None
