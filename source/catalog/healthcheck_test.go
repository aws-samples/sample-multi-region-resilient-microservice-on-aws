// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestHealthcheckURL(t *testing.T) {
	if got := healthcheckURL(""); got != "http://127.0.0.1:8080/health" {
		t.Errorf("default port: got %q", got)
	}
	if got := healthcheckURL("9090"); got != "http://127.0.0.1:9090/health" {
		t.Errorf("PORT=9090: got %q", got)
	}
}

func TestHealthcheckPassesOn200(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/health" {
			t.Errorf("probed %q, want /health", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	if code := healthcheck(srv.URL+"/health", time.Second); code != 0 {
		t.Errorf("exit code %d, want 0", code)
	}
}

func TestHealthcheckFailsOnErrorStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()
	if code := healthcheck(srv.URL+"/health", time.Second); code != 1 {
		t.Errorf("exit code %d, want 1", code)
	}
}

func TestHealthcheckFailsWhenNothingListens(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url := srv.URL + "/health"
	srv.Close()
	if code := healthcheck(url, time.Second); code != 1 {
		t.Errorf("exit code %d, want 1", code)
	}
}

func TestHealthcheckFailsWhenTheServerHangs(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
	}))
	defer srv.Close()
	defer close(release)
	start := time.Now()
	if code := healthcheck(srv.URL+"/health", 100*time.Millisecond); code != 1 {
		t.Errorf("exit code %d, want 1", code)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("took %v, want the timeout to bound it", elapsed)
	}
}
