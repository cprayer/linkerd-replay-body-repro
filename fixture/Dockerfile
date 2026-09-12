FROM golang:1.25.1 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go echo.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /audit-h2 .

FROM busybox:1.37
COPY --from=build /audit-h2 /audit-h2
ENTRYPOINT ["/audit-h2"]
