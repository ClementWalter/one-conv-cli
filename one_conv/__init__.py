"""Shared conversation contracts independent of CLI and authentication acquisition."""

# Sent on relay and archive requests: Cloudflare refuses Python's default "Python-urllib" agent (error 1010).
USER_AGENT = "one-conv"
