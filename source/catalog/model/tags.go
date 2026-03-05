// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

package model

// Tag exported
type Tag struct {
	Name        string `json:"name" db:"name"`
	DisplayName string `json:"displayName" db:"display_name"`
}
