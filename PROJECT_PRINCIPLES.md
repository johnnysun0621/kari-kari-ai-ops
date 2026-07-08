# Project Principles

## Product Direction

This project is not a one-off KariKari custom bot.

KariKari is the first testing and implementation site, but the product should be designed as a reusable operations scheduling system that can be applied to multiple companies, teams, and workflows.

Every new feature should be evaluated against this principle:

> Can this be configured for another company without rewriting core business logic?

## Core Product Model

The reusable product is an operations scheduling layer with these modules:

- Time collection
- Availability pool cleanup and deduplication
- Scheduling requests
- Candidate time matching
- Final confirmation
- Announcement and notification
- Audit log

Discord is the current interaction layer.
Notion is the current operations data layer.
Neither should be treated as permanently hard-coded product assumptions.

## Configuration First

Prefer configuration over hard-coded company-specific logic.

Company-specific values should live in environment variables, config files, or schema mapping layers:

- Discord guild IDs
- Discord channel IDs
- Discord role IDs
- Notion data source IDs
- Notion property names
- Course or activity types
- SLA rules
- Collection windows and deadlines
- Terminology used in customer-facing text

Avoid adding new core logic that only works because KariKari happens to use a specific page, property name, role name, or business term.

## Terminology

The current KariKari implementation uses terms like "藝人" because that matches the first customer workflow.

Long term, this concept should be treated as a generic schedulable participant:

- Talent
- Member
- Staff
- Participant

Use customer-specific terms in presentation/configuration, not in reusable domain logic where practical.

## Notion Schema Direction

The current repository layer maps directly to KariKari's Chinese Notion properties.

This is acceptable for the MVP, but future work should move toward schema mapping, such as:

- `artist_name_field`
- `discord_user_id_field`
- `contract_status_field`
- `availability_start_field`
- `availability_end_field`
- `schedule_status_field`

The goal is to let a new company connect its own Notion workspace by updating mapping/configuration rather than editing the core scheduling code.

## Discord Interaction Direction

Discord commands and UI should stay workflow-oriented rather than company-specific.

Good examples:

- Collect availability
- View my slots
- Clean availability pool
- Create schedule request
- Match candidate times
- Confirm final schedule

Avoid encoding company-specific processes into command behavior unless the behavior is configurable.

## Data Safety

Scheduling data can affect real people and paid work. Favor explicit, reversible operations.

Current principles:

- Preview destructive cleanup before applying it.
- Archive Notion pages instead of deleting them.
- Write audit logs for important changes.
- Keep user-submitted time private in Discord unless intentionally announced.
- Treat duplicate cleanup as deterministic and test-covered.

## Development Rules

When adding or changing features:

1. Keep KariKari working as the active test implementation.
2. Avoid making the core harder to reuse for a second company.
3. Put company-specific decisions behind config or mapping when the extra complexity is reasonable.
4. Add tests for scheduling rules, deduplication, matching, and parsing behavior.
5. Document product-level decisions that future agents or engineers need to preserve.

## Current State

KariKari is still the active P0 testing source.

The codebase is not fully multi-tenant yet. The next major productization step is extracting Notion property names and customer terminology out of `services/repositories.py` and related Discord presentation code into an explicit schema/config layer.
