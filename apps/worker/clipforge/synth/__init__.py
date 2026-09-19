"""Making a video that did not exist.

The harvest pipeline cuts video that already exists; this package writes,
speaks and draws one. The pieces here are pure: a prompt, a scene format,
and the arithmetic that times scenes against a narration. Drawing lives in
:mod:`clipforge.media.cartoon`, speaking in :mod:`clipforge.media.speech`, and
the stages that run them in :mod:`clipforge.stages.compose`.
"""
