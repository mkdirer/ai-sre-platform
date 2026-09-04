# Integration tests

Integration tests use the Compose-created `aisre_test` database for destructive empty/prior
revision migrations, payment persistence, concurrent incident deduplication, and durable job state.
They use live gateway/payment APIs for the real service-chain check. Run them only after the Compose
services are healthy:

```bash
make test-integration
```

The migration fixture resets only the `public` schema in `aisre_test`; it never targets the runtime
`aisre` database.
