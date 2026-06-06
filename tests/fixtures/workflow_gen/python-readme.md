# Python API Service

A REST API built with FastAPI for internal tooling.

## Tech Stack

- Python 3.11+
- FastAPI
- PostgreSQL

## Development

Install dependencies and run the API locally.

```bash
pip install -e .
uvicorn app.main:app --reload
```

## Testing

Run the test suite before merging:

```bash
python -m pytest tests/ -v
```
