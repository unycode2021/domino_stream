"""Bench commands. Loaded as ``domino_stream.commands``."""

import click


@click.command("domino-stream-setup")
def domino_stream_setup():
	"""Install MediaMTX and ffmpeg, and wire supervisor, Procfile, and nginx."""
	from domino_stream.runtime import ensure_runtime

	result = ensure_runtime()
	for note in result.notes:
		click.echo(note)
	for error in result.errors:
		click.echo(error, err=True)
	if result.errors:
		raise SystemExit(1)


commands = [domino_stream_setup]
