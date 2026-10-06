# Upgrade

Releases are listed on the [Releases page](https://github.com/lotus-infosec/weightpicks/releases), each with its database migrations and image digest. Read the notes first.

## One command

```bash
cd /opt/weightpicks
sudo ./scripts/upgrade.sh 1.1.0
```

It:

1. checks that both containers are healthy and the ledger verifies (nothing changes if not);
2. takes a backup labelled `pre-1.1.0` and verifies it;
3. checks out the release's files (`git checkout v1.1.0`) if the clone has no local changes;
4. sets `WP_VERSION=1.1.0` in `.env` and pulls the new image;
5. restarts: the web container applies migrations before serving, the worker waits for them;
6. waits until both are healthy and verifies the ledger again.

If anything fails it stops and prints the exact commands to go back. Installed with `--build`? Use `sudo git checkout v1.1.0 && sudo ./scripts/upgrade.sh --build`.

## Roll back

```bash
cd /opt/weightpicks
sudo git checkout v1.0.0
sudo sed -i 's/^WP_VERSION=.*/WP_VERSION=1.0.0/' .env
sudo docker compose up -d
```

If the newer version already migrated the database, also restore the backup it took (database downgrades aren't supported):

```bash
sudo docker compose exec worker wp backup list
sudo docker compose exec worker wp maintenance restore wp-…-pre-1-1-0.tar.gz
sudo docker compose restart
```

## Verify a release image (optional)

Images are built by GitHub Actions from signed tags, with build provenance:

```bash
gh attestation verify oci://ghcr.io/lotus-infosec/weightpicks:1.1.0 --owner lotus-infosec
```
