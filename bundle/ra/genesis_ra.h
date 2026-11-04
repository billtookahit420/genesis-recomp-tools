#pragma once

/* GenesisRecomp Tools — RetroAchievements hooks for segagenesisrecomp
 * game projects. The module (genesis_ra.cpp) is compiled into the game
 * only when a conversion runs with RetroAchievements on; the engine
 * wires it in through two small, marker-guarded patches:
 *
 *   - the game's spec translation unit (game/sonic1_spec.c) points
 *     GameSpec.on_frame_post at genesis_ra_frame_post(), and
 *   - the staged runner's main.c calls genesis_ra_bind_ui() right
 *     after it creates the standard runtime UI, handing the module
 *     the handle its toast channel needs.
 *
 * With RA off, neither patch is applied and this header is unused.
 */
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Called once from the patched runner with the RecompRuntimeUi*
 * created for this process (stored as void* so the C runner does not
 * need the recomp-ui headers). */
void genesis_ra_bind_ui(void *ui);

/* GameSpec.on_frame_post hook: lazily initialises the RA client on
 * the first frame, pumps rc_client once per frame, delivers queued
 * server responses on the game thread, and maintains the toast. */
void genesis_ra_frame_post(uint64_t frame_index);

#ifdef __cplusplus
}
#endif
