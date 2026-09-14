"""Publishing: the publish gate, worker-held credentials, and the YouTube client.

The dependency direction here is deliberate. :mod:`gate` imports nothing from
:mod:`youtube` or :mod:`credentials`, so it can be reasoned about — and tested —
with no network, no tokens and no platform at all. A gate that needed a YouTube
client to answer "may this be published?" would be a gate nobody could exercise
in CI.
"""
