.PHONY: run test lint check

run:
	go run ./cmd/server

test:
	go test ./...

lint:
	go tool golangci-lint run

# Зовётся из корневого ./check (pre-commit, ./finish). Красный → коммит отменяется.
check:
	go build ./...
	go vet ./...
	go tool golangci-lint run
	go test -short ./...
