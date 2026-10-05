// @ts-check
import { test, expect } from '@playwright/test';
import { navTo } from './nav-helper.js';

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-initial-refresh-complete', 'true', { timeout: 15_000 });
  await navTo(page, 'view-flow');
  await expect(page.locator('#flow-graph .flow-node').first()).toBeVisible();
});

test('Flow canvas zoom is bounded, keyboard accessible, and honestly reported', async ({ page }) => {
  const viewport = page.locator('#flow-viewport');
  const value = page.locator('#flow-zoom-value');
  await expect(value).toHaveText('100%');
  expect(await value.evaluate((node) => node.matches('output, [role="status"], [aria-live]:not([aria-live="off"])'))).toBe(false);

  await page.getByRole('button', { name: 'Zoom in flow canvas' }).click();
  await expect(value).toHaveText('110%');
  await expect(page.locator('#flow-stage')).toHaveCSS('transform', /matrix\(1\.1/);

  await viewport.focus();
  await page.keyboard.press('-');
  await expect(value).toHaveText('100%');

  for (let i = 0; i < 5; i += 1) await page.getByRole('button', { name: 'Zoom out flow canvas' }).click();
  await expect(value).toHaveText('50%');
  await expect(page.getByRole('button', { name: 'Zoom out flow canvas' })).toBeDisabled();

  for (let i = 0; i < 11; i += 1) await page.getByRole('button', { name: 'Zoom in flow canvas' }).click();
  await expect(value).toHaveText('160%');
  await expect(page.getByRole('button', { name: 'Zoom in flow canvas' })).toBeDisabled();
  await expect(page.locator('#flow-canvas-status')).toContainText('160%');

  await viewport.focus();
  await page.keyboard.press('0');
  await expect(value).toHaveText('100%');
});

test('Flow minimap is rendered from real nodes and tracks the visible region', async ({ page }) => {
  const nodes = page.locator('#flow-graph .flow-node');
  const minimapNodes = page.locator('#flow-minimap-svg .flow-minimap-node');
  await expect(minimapNodes).toHaveCount(await nodes.count());
  await expect(page.locator('#flow-minimap-svg .flow-minimap-edge')).toHaveCount(1);

  const before = await page.locator('#flow-minimap-viewport').evaluate((node) => ({
    top: parseFloat(node.style.top), left: parseFloat(node.style.left),
  }));
  await page.locator('#flow-viewport').evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect.poll(() => page.locator('#flow-minimap-viewport').evaluate((node) => parseFloat(node.style.top)))
    .toBeGreaterThan(before.top);

  await page.locator('#flow-minimap').click({ position: { x: 4, y: 4 } });
  await expect.poll(() => page.locator('#flow-viewport').evaluate((node) => node.scrollTop)).toBeLessThan(8);

  await page.locator('#flow-minimap').focus();
  await page.keyboard.press('End');
  await expect.poll(() => page.locator('#flow-viewport').evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
  await page.keyboard.press('Home');
  await expect.poll(() => page.locator('#flow-viewport').evaluate((node) => node.scrollTop)).toBeLessThan(8);

  await page.getByRole('button', { name: 'Fit flow canvas to screen' }).click();
  const fitted = Number((await page.locator('#flow-zoom-value').textContent()).replace('%', ''));
  expect(fitted).toBeGreaterThanOrEqual(50);
  expect(fitted).toBeLessThanOrEqual(100);
  const fitState = await page.evaluate(() => {
    const viewport = document.querySelector('#flow-viewport');
    const stage = document.querySelector('#flow-stage');
    const status = document.querySelector('#flow-canvas-status');
    if (!(viewport instanceof HTMLElement) || !(stage instanceof HTMLElement)) return null;
    const style = getComputedStyle(viewport);
    const viewportRect = viewport.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    return {
      honest: status?.textContent?.includes('fitted') || status?.textContent?.includes('requires scrolling'),
      widthFits: stageRect.width <= viewportRect.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) + 1,
      heightFits: stageRect.height <= viewportRect.height - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom) + 1,
      atMinimum: document.querySelector('#flow-zoom-out')?.hasAttribute('disabled') ?? false,
    };
  });
  expect(fitState?.honest).toBe(true);
  if (!fitState?.atMinimum) {
    expect(fitState?.widthFits).toBe(true);
    expect(fitState?.heightFits).toBe(true);
  }
});

test('Flow edges stay attached after zoom and expansion; fullscreen is a real browser request', async ({ page }) => {
  async function alignment() {
    return page.evaluate(() => {
      const path = document.querySelector('#flow-edges-svg path');
      const from = document.querySelector('[data-task-id="fx-running-1"] > summary');
      if (!(path instanceof SVGPathElement) || !(from instanceof HTMLElement)) return null;
      const point = path.getPointAtLength(0);
      const matrix = path.getScreenCTM();
      const screen = matrix ? new DOMPoint(point.x, point.y).matrixTransform(matrix) : point;
      const rect = from.getBoundingClientRect();
      return { dx: Math.abs(screen.x - rect.right), dy: Math.abs(screen.y - (rect.top + rect.height / 2)) };
    });
  }

  await expect.poll(alignment).not.toBeNull();
  let aligned = await alignment();
  expect(aligned.dx).toBeLessThan(3);
  expect(aligned.dy).toBeLessThan(3);

  await page.getByRole('button', { name: 'Zoom in flow canvas' }).click();
  await page.locator('[data-task-id="fx-running-1"] > summary').click();
  await expect.poll(async () => (await alignment()).dx).toBeLessThan(3);
  await expect.poll(async () => (await alignment()).dy).toBeLessThan(3);
  aligned = await alignment();
  expect(aligned.dx).toBeLessThan(3);
  expect(aligned.dy).toBeLessThan(3);

  const fullscreen = page.locator('#flow-fullscreen');
  if (await fullscreen.isDisabled()) {
    await expect(fullscreen).toHaveAttribute('aria-label', 'Flow canvas fullscreen unavailable');
  } else {
    await fullscreen.click();
    await expect.poll(() => page.evaluate(() => document.fullscreenElement?.id)).toBe('flow-canvas-card');
    const fullscreenLayout = await page.locator('#flow-canvas-card').evaluate((card) => {
      const canvas = card.querySelector('#flow-canvas');
      if (!(canvas instanceof HTMLElement)) return null;
      const cardRect = card.getBoundingClientRect();
      const canvasRect = canvas.getBoundingClientRect();
      return {
        cardWithinViewport: cardRect.bottom <= innerHeight + 1 && cardRect.right <= innerWidth + 1,
        canvasWithinCard: canvasRect.bottom <= cardRect.bottom + 1 && canvasRect.right <= cardRect.right + 1,
      };
    });
    expect(fullscreenLayout?.cardWithinViewport).toBe(true);
    expect(fullscreenLayout?.canvasWithinCard).toBe(true);
    await page.evaluate(() => document.exitFullscreen());
    await expect.poll(() => page.evaluate(() => document.fullscreenElement?.id || null)).toBeNull();
  }

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
