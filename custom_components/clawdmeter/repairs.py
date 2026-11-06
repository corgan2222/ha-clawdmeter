"""Repairs for the Clawdmeter integration."""

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import CONF_PLAN, DOMAIN

ISSUE_STALE_ENTITIES = "stale_entities"


@callback
def async_create_stale_entities_issue(
    hass: HomeAssistant, entry: ConfigEntry, old_plan: str, entity_ids: list[str]
) -> None:
    """Offer to remove the sensors a plan change left without data.

    Whether they go is the user's decision, so the issue only lists them and the
    fix flow removes or keeps them as chosen. The change is detected once, which
    is why the issue is persistent.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_STALE_ENTITIES}_{entry.entry_id}",
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_STALE_ENTITIES,
        translation_placeholders={
            "old_plan": old_plan,
            "new_plan": entry.data.get(CONF_PLAN) or "?",
            "entities": "\n".join(f"- `{entity_id}`" for entity_id in entity_ids),
        },
        data={"entity_ids": ",".join(entity_ids)},
    )


class StaleEntitiesRepairFlow(RepairsFlow):
    """Let the user remove the listed entities or keep them."""

    def __init__(self, issue_id: str, entity_ids: list[str]) -> None:
        """Remember the issue and the entities it offered to remove."""
        self._issue_id = issue_id
        self._entity_ids = entity_ids

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show the list with the choice to remove or keep them.

        A fixable issue gets no ignore button from the frontend, so keeping the
        sensors is offered here, the same way core integrations do it.
        """
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self._issue_id)
        return self.async_show_menu(
            step_id="init",
            menu_options=["confirm", "ignore"],
            description_placeholders=issue.translation_placeholders if issue else None,
        )

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Remove the entities."""
        registry = er.async_get(self.hass)
        for entity_id in self._entity_ids:
            if registry.async_get(entity_id) is not None:
                registry.async_remove(entity_id)
        return self.async_create_entry(data={})

    async def async_step_ignore(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Keep the entities and ignore the issue."""
        ir.async_ignore_issue(self.hass, DOMAIN, self._issue_id, True)
        return self.async_abort(reason="ignored")


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the fix flow for a Clawdmeter issue."""
    entity_ids = (data or {}).get("entity_ids") or ""
    return StaleEntitiesRepairFlow(issue_id, [e for e in entity_ids.split(",") if e])
