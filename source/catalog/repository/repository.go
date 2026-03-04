package repository

import (
	"catalog/config"
	"catalog/model"
	"context"
	"fmt"

	"github.com/prometheus/client_golang/prometheus"
)

type Repository interface {
	List(tags []string, order string, pageNum, pageSize int, ctx context.Context) ([]model.Product, error)
	Count(tags []string, ctx context.Context) (int, error)
	Get(id string, ctx context.Context) (*model.Product, error)
	Tags(ctx context.Context) ([]model.Tag, error)
	Collector() prometheus.Collector
	ReaderCollector() prometheus.Collector
}

func NewRepository(config config.DatabaseConfiguration) (Repository, error) {
	if config.Type == "mysql" {
		return newMySQLRepository(config)
	}

	return nil, fmt.Errorf("Unknown database type: %s", config.Type)
}
