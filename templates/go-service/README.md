# Шаблон Go-сервиса

Корневой `./check` сам находит `app/<svc>/Makefile` и гоняет `make check`:
build → vet → golangci-lint → `go test -short`.

```sh
mkdir -p app && cp -R templates/go-service app/<svc>
cd app/<svc>
go mod init <module>/app/<svc>
go get -tool github.com/golangci/golangci-lint/v2/cmd/golangci-lint
mkdir -p cmd/server   # main.go — точка входа для `make run`
```

- golangci-lint подключён как `tool` в go.mod (Go ≥1.24): версия фиксируется
  в go.sum, отдельной установки не нужно, `go tool golangci-lint` работает
  одинаково у всех.
- `.golangci.yml` — формат v2, форматтеры gofmt + goimports. Линтеры
  добавляй в `linters.enable`.
- Тесты, которым нужны внешние зависимости (testcontainers, сеть), помечай
  `if testing.Short() { t.Skip() }` — `make check` их пропускает.
- Сеть sandbox'а: `proxy.golang.org`, `sum.golang.org` уже в
  `.claude/settings.json`; приватные модули — добавить домен туда.
