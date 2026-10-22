# Contributing Guidelines

Thank you for your interest in contributing to the Aurora Fleet Patching Agent! We welcome community contributions.

## How to Contribute

### Reporting Issues

- Use GitHub Issues to report bugs or request features
- Include steps to reproduce, expected behavior, and actual behavior
- Include your Aurora engine type (PostgreSQL/MySQL), version, and region

### Pull Requests

1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes
4. Run tests: `python -m pytest tests/ -v`
5. Ensure code passes linting: `ruff check .`
6. Submit a pull request

### Development Setup

```bash
# Clone and install
git clone https://github.com/aws-samples/sample-agentic-aurora-fleet-patching.git
cd sample-agentic-aurora-fleet-patching
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -v

# Run linter
ruff check .

# Build UI
cd ui && npm install && npm run build && cd ..

# Run locally
cd api && uvicorn server:app --port 8000 --reload
```

### Code Style

- Python: Follow PEP 8, enforced by `ruff`
- Use type hints for function signatures
- Docstrings for all public functions
- Keep agent handlers single-purpose and stateless

### Areas for Contribution

- Additional database engine support (Aurora DSQL, RDS)
- New CVE data sources
- UI improvements
- Test coverage expansion
- Documentation improvements
- Performance optimizations
- New pre-flight check rules

## Security

If you discover a potential security issue, please notify AWS Security via our
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/).
Please do **not** create a public GitHub issue for security vulnerabilities.
See [SECURITY.md](SECURITY.md) for details.

## License

By contributing, you agree that your contributions will be licensed under the MIT-0 License.
