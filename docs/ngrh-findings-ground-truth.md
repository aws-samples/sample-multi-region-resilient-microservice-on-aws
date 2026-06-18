# NGRH Findings Ground Truth

> Baseline assessment results for the AWS Resilience Hub (NGRH v2) model
> configured on this repository. Use this document to evaluate whether future
> assessment runs, agent-driven remediation, or architecture changes improve,
> regress, or maintain the resilience posture.

## Metadata

| Field | Value |
|-------|-------|
| Assessment date | 2026-06-17 |
| Account | 968715863728 |
| Stack | ngrh-ngrh |
| Services | 6 (ui, catalog, cart, checkout, orders, assets) |
| Policies | TierOne (RTO=10 min, RPO=0) · TierTwo (RTO=10 min, RPO=5 min) |
| Tier1 services | ui, cart, orders |
| Tier2 services | catalog, checkout, assets |
| Total findings | 35 |
| Assertions active | 29 (suppress known-acceptable patterns) |
| Reduction from baseline (no assertions) | 54% (76 → 35) |

## Architecture Context

- **Deployment**: Active/Active, us-east-1 (primary) + us-west-2 (standby)
- **Compute**: ECS Fargate (FARGATE_SPOT only — design choice for cost, not production recommendation)
- **ARC Region Switch**: Automated triggers via composite alarm (4 remote canaries ALARM + local healthy OK, 5 min sustained)
- **Pre-scaling**: 200% of containerInsightsMaxInLast24Hours before failover proceeds
- **Data**: Aurora Global DB (catalog), DynamoDB Global Table (cart), Aurora DSQL (orders), ElastiCache Redis (checkout, ephemeral), RabbitMQ (orders, SINGLE_INSTANCE)

## Findings by Service

### ui (6 findings) — TierOne

| # | Finding | Category | Notes |
|---|---------|----------|-------|
| 1 | ECS services use FARGATE_SPOT exclusively risking capacity loss during failover | Compute | Design choice for sample — real workloads should use mixed FARGATE + FARGATE_SPOT |
| 2 | ALB client keep-alive at default 3600 seconds delays recovery from impaired nodes | Networking | Valid — production should tune idle timeout |
| 3 | Route 53 hosted zone lacks accelerated recovery slowing DNS failover propagation | DNS | Valid — consider Route 53 ARC or lower TTLs |
| 4 | ALB deletion protection disabled on production load balancers | Protection | Valid for production; intentionally off for sample teardown automation |
| 5 | ALB target groups lack minimum healthy targets threshold risking congestive collapse | Load balancing | Valid — set deregistration delay + min healthy targets |
| 6 | UI targets span only 2 of 3 AZs risking 50% capacity loss during AZ impairment | Availability | Architecture constraint (VPC has 2 AZs in sample) |

### catalog (9 findings) — TierTwo

| # | Finding | Category | Notes |
|---|---------|----------|-------|
| 1 | ARC Region Switch plan sequential execution risks exceeding 10-minute RTO | Failover | Valid concern — depends on step timeouts; parallel steps mitigate |
| 2 | ALB default keep-alive delays recovery from impaired load balancer nodes | Networking | Same as ui #2 |
| 3 | ALB deletion protection disabled risks accidental infrastructure loss | Protection | Same rationale as ui #4 |
| 4 | Catalog service directs all reads to writer endpoint wasting reader capacity | Database | Valid — Go app uses writer endpoint; should use reader for queries |
| 5 | ECS catalog targets in only 2 AZs risks 50% capacity loss during AZ impairment | Availability | Same as ui #6 |
| 6 | Aurora Global Database lacks immutable backup protection against catastrophic deletion | Data protection | Valid — no AWS Backup vault lock configured |
| 7 | ALB target group lacks fail-open protection against coordinated health check failures | Load balancing | Valid — configure fail-open |
| 8 | Aurora cluster lacks read capacity redundancy after AZ failover | Database | Valid — single reader replica |
| 9 | ECS services use FARGATE_SPOT exclusively risking capacity loss during failover | Compute | Same as ui #1 |

### cart (6 findings) — TierOne

| # | Finding | Category | Notes |
|---|---------|----------|-------|
| 1 | DynamoDB Global Table lacks deletion protection enabling accidental data loss | Data protection | Valid for production; off for sample teardown |
| 2 | ALB client keep-alive at default 3600 seconds delays impaired-node drainage | Networking | Same pattern |
| 3 | ALB deletion protection disabled risking accidental infrastructure removal | Protection | Same rationale |
| 4 | ALB Target Groups lack fail-open routing during coordinated health check failures | Load balancing | Valid |
| 5 | ECS carts services rely exclusively on FARGATE_SPOT risking capacity loss | Compute | Same as ui #1 |
| 6 | No AWS Backup plan protecting DynamoDB table against correlated data destruction | Data protection | Valid — no backup configured for DDB |

### checkout (6 findings) — TierTwo

| # | Finding | Category | Notes |
|---|---------|----------|-------|
| 1 | Checkout session state lost on regional failover with no cross-region replication | Data durability | **Expected** — checkout uses ephemeral ElastiCache; RPO=5 accepts session loss. Assertion documents this. |
| 2 | ALB deletion protection disabled risks accidental infrastructure removal | Protection | Same rationale |
| 3 | FARGATE_SPOT exclusive capacity risks task interruptions during demand spikes | Compute | Same as ui #1 |
| 4 | ElastiCache Redis lacks replicas and automatic failover in both regions | Data/HA | Valid — single-node Redis is a cost choice for sample |
| 5 | ARC Region Switch sequential step timeouts may exceed 10-minute RTO | Failover | Same as catalog #1 |
| 6 | No backup snapshots configured for checkout session state in Redis | Data protection | **Expected** — ephemeral state, RPO=5 accepts loss |

### orders (7 findings) — TierOne

| # | Finding | Category | Notes |
|---|---------|----------|-------|
| 1 | RabbitMQ brokers deployed as SINGLE_INSTANCE lack within-region high availability | Messaging | Valid — production should use ACTIVE_STANDBY_MULTI_AZ |
| 2 | No backup plan protects Aurora DSQL against logical corruption or accidental deletion | Data protection | Valid — DSQL is durable but no point-in-time recovery configured |
| 3 | ALB target groups lack fail-open routing for coordinated health check failures | Load balancing | Valid |
| 4 | Aurora DSQL clusters and production ALBs lack deletion protection | Protection | Same teardown rationale |
| 5 | ALB default keep-alive of 3600 seconds delays recovery from impaired nodes | Networking | Same pattern |
| 6 | ECS services use exclusively FARGATE_SPOT with no on-demand baseline | Compute | Same as ui #1 |
| 7 | ALB targets in only 2 AZs risks 50% capacity loss during AZ impairment | Availability | Same as ui #6 |

### assets (1 finding) — TierTwo

| # | Finding | Category | Notes |
|---|---------|----------|-------|
| 1 | ECS services use FARGATE_SPOT exclusively risking task interruptions | Compute | Same as ui #1 |

## Finding Categories Summary

| Category | Count | Systemic? |
|----------|-------|-----------|
| Compute (FARGATE_SPOT) | 6 | Yes — all services, by design |
| Protection (deletion protection off) | 5 | Yes — all ALBs + DDB + DSQL, by design for teardown |
| Load balancing (fail-open, min healthy) | 5 | Yes — ALB pattern |
| Networking (keep-alive) | 4 | Yes — ALB default |
| Availability (2 AZ) | 3 | Yes — VPC design |
| Data protection (no backup) | 3 | Mixed — DDB/Aurora/Redis |
| Failover (RTO risk) | 2 | ARC plan timing |
| Database | 2 | Catalog-specific |
| Data durability | 1 | Expected (checkout ephemeral) |
| DNS | 1 | Route 53 |
| Messaging | 1 | RabbitMQ single-instance |

## Evaluation Criteria for Future Runs

### Improvement (findings decrease)
- Adding FARGATE on-demand base capacity → removes 6 FARGATE_SPOT findings
- Enabling deletion protection → removes 5 protection findings
- Moving to 3-AZ VPC → removes 3 AZ findings
- Configuring ALB idle timeout → removes 4 keep-alive findings
- Adding AWS Backup plans → removes 3 backup findings

### Regression (findings increase)
- New findings in previously-clean areas indicate architecture degradation
- Finding count > 35 without new services = regression
- Same finding appearing with higher severity language = possible LLM non-determinism (re-run to confirm)

### Expected Variation
- NGRH findings are **LLM-generated and non-deterministic**. Exact wording may vary across runs.
- Finding **counts** are more stable than finding **prose**.
- A ±2 variance in total count is normal between runs with identical architecture.
- Finding IDs regenerate every assessment — do not compare by ID.

## Notes on Assertions

29 assertions are active to suppress known-acceptable patterns:
- "Does not use Aurora" negative assertions on non-catalog services
- "AZ failover by design" assertions on all services
- "Post-commit-only MQ" assertion on orders
- "No data stores" assertion on assets
- Shared-infra exclusion assertions (ElastiCache/MQ discovered via shared apps stack)

Without assertions, the baseline is **76 findings** (many are false-positive cross-attributions from shared infrastructure discovery).
