"""A local data agent: proposes SQL, is not trusted to execute it.

Component boundaries mirror docs/architecture: the gateway decides, the
evidence store remembers, and nothing else touches the warehouse.
"""
