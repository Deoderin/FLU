# Випуск нової версії

Код живе в репозиторії, зібрана програма — у **Releases** того самого репозиторію на GitHub:
кожна версія — реліз `v<версія>` з прикріпленим `UGV-Quick-Setup-<версія>.zip`. Сам архів у git не комітиться
(`dist/` у `.gitignore`).

## Кроки

1. **Версія.** Підніміть `__version__` у `fleet/__init__.py` (наприклад `0.3.0` → `0.3.1` для виправлень,
   `0.4.0` для нового) і додайте розділ у `CHANGELOG.md`.
2. **Збірка.** Двічі клацніть `build-exe.cmd`. Результат: `dist\UGV Quick Setup\` і `dist\UGV-Quick-Setup-<версія>.zip`.
3. **Перевірка.** Розпакуйте zip у тимчасову теку з латинським шляхом і запустіть `UGV Quick Setup.exe`:
   у заголовку вікна — нова версія, майстер відкривається.
4. **Коміт.** Закомітьте й відправте зміни через GitHub Desktop.
5. **Реліз.** На github.com → репозиторій → **Releases** → **Draft a new release**:
   - **Choose a tag**: `v<версія>` → «Create new tag: v<версія> on publish», **Target**: `main`;
   - **Release title**: `UGV Quick Setup <версія>`;
   - **опис**: розділ цієї версії з `CHANGELOG.md`;
   - перетягніть `dist\UGV-Quick-Setup-<версія>.zip` у поле **Attach binaries**;
   - **Publish release**.

Те саме з GitHub CLI, якщо він встановлений і ви увійшли (`gh auth login`):

```
gh release create v0.3.0 "dist\UGV-Quick-Setup-0.3.0.zip" --title "UGV Quick Setup 0.3.0" --notes "див. CHANGELOG.md"
```

## Кому й як передавати

Репозиторій приватний: реліз можуть завантажити лише ті, кому відкрито доступ. Замовнику передавайте сам zip
з релізу. У збірку потрапляють лише шаблони — ваші `inventory\` і `secrets\` лишаються на вашому ноутбуці.

Оновлення в замовника: закрити програму, розпакувати новий zip **поверх** старої теки (inventory\, secrets\
і build\ лишаються на місці) і запустити `UGV Quick Setup.exe`.
