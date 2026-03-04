package api

import (
	"catalog/model"
	"catalog/repository"
	"context"
)

// CatalogAPI type
type CatalogAPI struct {
	repository repository.Repository
}

func (a *CatalogAPI) GetProducts(tags []string, order string, pageNum, pageSize int, ctx context.Context) ([]model.Product, error) {
	return a.repository.List(tags, order, pageNum, pageSize, ctx)
}

func (a *CatalogAPI) GetProduct(id string, ctx context.Context) (*model.Product, error) {
	return a.repository.Get(id, ctx)
}

func (a *CatalogAPI) GetTags(ctx context.Context) ([]model.Tag, error) {
	return a.repository.Tags(ctx)
}

func (a *CatalogAPI) GetSize(tags []string, ctx context.Context) (int, error) {
	return a.repository.Count(tags, ctx)
}

// NewCatalogAPI constructor
func NewCatalogAPI(repository repository.Repository) (*CatalogAPI, error) {
	/*repository, err := repository.NewRepository(configuration)
	if err != nil {
		log.Println("Error creating catalog API", err)
		return nil, err
	}*/

	return &CatalogAPI{
		repository: repository,
	}, nil
}
