package main

import (
	"catalog/api"
	"catalog/config"
	"catalog/controller"
	"catalog/repository"
	"context"
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/sethvargo/go-envconfig"
	swaggerFiles "github.com/swaggo/files"
	ginSwagger "github.com/swaggo/gin-swagger"
	ginprometheus "github.com/zsais/go-gin-prometheus"
	"go.opentelemetry.io/contrib/detectors/aws/ec2"
	"go.opentelemetry.io/contrib/instrumentation/github.com/gin-gonic/gin/otelgin"
	"go.opentelemetry.io/contrib/propagators/aws/xray"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

func main() {
	ctx := context.Background()

	var config config.AppConfiguration
	if err := envconfig.Process(ctx, &config); err != nil {
		log.Fatal(err)
	}

	db, err := repository.NewRepository(config.Database)
	if err != nil {
		log.Fatal(err)
	}
	api, err := api.NewCatalogAPI(db)
	//api, err := api.NewCatalogAPI()
	if err != nil {
		log.Fatal(err)
	}

	r := gin.New()
	r.Use(gin.LoggerWithConfig(gin.LoggerConfig{
		SkipPaths: []string{"/health"},
	}))

	p := ginprometheus.NewPrometheus("gin")
	p.Use(r)

	c, err := controller.NewController(api)
	if err != nil {
		log.Fatalln("Error creating controller", err)
	}

	catalog := r.Group("/catalogue")

	_, otelPresent := os.LookupEnv("OTEL_SERVICE_NAME")

	if otelPresent {
		tp, err := initTracer(ctx)
		if err != nil {
			log.Fatal(err)
		}
		catalog.Use(otelgin.Middleware("catalog-server", otelgin.WithTracerProvider(tp)))
	}

	catalog.GET("", c.GetProducts)

	catalog.GET("/size", c.CatalogSize)
	catalog.GET("/tags", c.ListTags)
	catalog.GET("/product/:id", c.GetProduct)

	r.GET("/swagger/*any", ginSwagger.WrapHandler(swaggerFiles.Handler))
	r.GET("/health", func(c *gin.Context) {
		c.String(http.StatusOK, "OK")
	})

	srv := &http.Server{
		Addr:    ":" + strconv.Itoa(config.Port),
		Handler: r,
	}

	// Initializing the server in a goroutine so that
	// it won't block the graceful shutdown handling below
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %s\n", err)
		}
	}()

	// Wait for interrupt signal to gracefully shutdown the server with
	// a timeout of 5 seconds.
	quit := make(chan os.Signal)
	// kill (no param) default send syscall.SIGTERM
	// kill -2 is syscall.SIGINT
	// kill -9 is syscall.SIGKILL but can't be catch, so don't need add it
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("Shutting down server...")

	// The context is used to inform the server it has 5 seconds to finish
	// the request it is currently handling
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatal("Server forced to shutdown:", err)
	}

	log.Println("Server exiting")
}

func initTracer(ctx context.Context) (*sdktrace.TracerProvider, error) {
	client := otlptracegrpc.NewClient()
	exporter, err := otlptrace.New(ctx, client)
	if err != nil {
		return nil, fmt.Errorf("creating OTLP trace exporter: %w", err)
	}
	idg := xray.NewIDGenerator()
	ec2ResourceDetector := ec2.NewResourceDetector()
	resource, err := ec2ResourceDetector.Detect(context.Background())
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithIDGenerator(idg),
		sdktrace.WithResource(resource),
	)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	return tp, nil
}
