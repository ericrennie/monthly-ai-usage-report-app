# Security

- Run the server only on its default loopback address (`127.0.0.1`).
- Prefer an AWS profile or single sign-on over manually entered credentials.
- When manual credentials are necessary, use temporary credentials only.
- Verify ownership of the AWS Lambda Function URL before approving egress.
- Never commit credentials, generated reports, or private dashboard exports.

Please report security issues privately to the repository owner instead of
opening a public issue containing credentials or internal URLs.
