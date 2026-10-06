"""Extraction: read the seven invoice fields from an image via AWS Bedrock.

The client is built lazily (see ``client.py``) so a replay run from cache
never touches the AWS credential chain.
"""
