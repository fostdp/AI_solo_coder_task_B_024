(function () {
    "use strict";

    var API_BASE = "";
    var devices = [];
    var deviceMap = {};
    var factories = [];
    var factoriesByRegion = [];
    var factoryMap = {};
    var lifePredictions = {};
    var currentView = "factory";
    var currentFactoryId = null;
    var showLifePrediction = true;
    var selectedDeviceId = null;
    var ws = null;
    var refreshTimer = null;
    var pendingUpdates = null;
    var rafId = null;
    var gridBuilt = false;

    var gridEl = document.getElementById("deviceGrid");
    var sidebarEl = document.getElementById("sidebar");
    var sidebarBody = document.getElementById("sidebarBody");
    var sidebarTitle = document.getElementById("sidebarTitle");
    var alertContainer = document.getElementById("alertContainer");
    var filterArea = document.getElementById("filterArea");
    var filterType = document.getElementById("filterType");
    var filterHealth = document.getElementById("filterHealth");
    var showLifePredictionCheckbox = document.getElementById("showLifePrediction");
    var factoryMapEl = document.getElementById("factoryMap");
    var factoryMapContainer = document.getElementById("factoryMapContainer");
    var deviceGridContainer = document.getElementById("deviceGridContainer");
    var backToFactoriesBtn = document.getElementById("backToFactories");
    var devicesLegendBar = document.getElementById("devicesLegendBar");
    var poModal = document.getElementById("purchaseOrderModal");

    var _healthStyleEl = null;

    function _ensureHealthStyles() {
        if (_healthStyleEl) return;
        _healthStyleEl = document.createElement("style");
        var css = "";
        for (var s = 0; s <= 100; s += 5) {
            var color = healthToColor(s);
            css += ".h-" + s + "{background:" + color + "!important;}";
        }
        css += ".h-null{background:#374151!important;}";
        _healthStyleEl.textContent = css;
        document.head.appendChild(_healthStyleEl);
    }

    function healthToColor(score) {
        if (score == null) return "#374151";
        score = Math.max(0, Math.min(100, score));
        var r, g, b;
        if (score < 50) {
            var t = score / 50;
            r = 239;
            g = Math.round(68 + (179 - 68) * t);
            b = Math.round(68 + (8 - 68) * t);
        } else {
            var t = (score - 50) / 50;
            r = Math.round(234 + (34 - 234) * t);
            g = Math.round(179 + (197 - 179) * t);
            b = Math.round(8 + (94 - 8) * t);
        }
        return "rgb(" + r + "," + g + "," + b + ")";
    }

    function healthBandClass(score) {
        if (score == null) return "h-null";
        var band = Math.round(Math.max(0, Math.min(100, score)) / 5) * 5;
        return "h-" + band;
    }

    function healthClass(score) {
        if (score == null) return "unknown";
        if (score >= 80) return "good";
        if (score >= 60) return "warn";
        return "danger";
    }

    function formatTime(ts) {
        if (!ts) return "--";
        var d = new Date(ts);
        return d.toLocaleString("zh-CN", {
            month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit"
        });
    }

    function updateClock() {
        var now = new Date();
        document.getElementById("headerTime").textContent =
            now.toLocaleString("zh-CN", {
                year: "numeric", month: "2-digit", day: "2-digit",
                hour: "2-digit", minute: "2-digit", second: "2-digit"
            });
    }

    function fetchJSON(url) {
        return fetch(url).then(function (r) {
            if (!r.ok) throw new Error(r.statusText);
            return r.json();
        });
    }

    function postJSON(url) {
        return fetch(url, { method: "POST" }).then(function (r) {
            if (!r.ok) throw new Error(r.statusText);
            return r.json();
        });
    }

    function loadSummary() {
        Promise.all([
            fetchJSON(API_BASE + "/api/dashboard/summary"),
            fetchJSON(API_BASE + "/api/factories")
        ]).then(function (results) {
            var data = results[0];
            var factoriesData = results[1];
            document.getElementById("totalFactories").textContent = factoriesData.length;
            document.getElementById("totalDevices").textContent = data.total_devices;
            document.getElementById("onlineDevices").textContent = data.online_devices;
            document.getElementById("avgHealth").textContent = data.avg_health_score;
            document.getElementById("lowHealthDevices").textContent = data.low_health_devices;
            document.getElementById("pendingOrders").textContent = data.pending_work_orders;
            document.getElementById("activeAlerts").textContent = data.active_alerts;
        }).catch(function () { });
    }

    function loadFactories() {
        fetchJSON(API_BASE + "/api/factories/aggregated").then(function (regionData) {
            factoriesByRegion = regionData;
            factories = [];
            factoryMap = {};
            regionData.forEach(function (r) {
                r.factories.forEach(function (f) {
                    factories.push(f);
                    factoryMap[f.factory_id] = f;
                });
            });
            renderFactoryMap(regionData);
        }).catch(function (err) {
            console.error("Failed to load factories:", err);
            fetchJSON(API_BASE + "/api/factories").then(function (data) {
                factories = data;
                factoryMap = {};
                data.forEach(function (f) { factoryMap[f.factory_id] = f; });
                factoriesByRegion = [{
                    region: "全部厂区",
                    factory_count: data.length,
                    total_devices: data.reduce(function (s, f) { return s + (f.device_count || 0); }, 0),
                    avg_health: data.reduce(function (s, f) { return s + ((f.avg_health || 0) * (f.device_count || 1)); }, 0) / data.length,
                    factories: data,
                    center_lat: data.length ? data[0].lat : 0,
                    center_lng: data.length ? data[0].lng : 0
                }];
                renderFactoryMap(factoriesByRegion);
            }).catch(function () {});
        });
    }

    function loadFactoryDevices(factoryId) {
        fetchJSON(API_BASE + "/api/factories/" + factoryId + "/devices").then(function (data) {
            devices = data;
            deviceMap = {};
            data.forEach(function (d) { deviceMap[d.device_id] = d; });
            populateFilters(data);
            if (!gridBuilt) {
                buildGrid(data);
                gridBuilt = true;
            } else {
                scheduleGridUpdate(data);
            }
        }).catch(function (err) {
            console.error("Failed to load factory devices:", err);
        });
    }

    function loadLifePredictions(factoryId) {
        fetchJSON(API_BASE + "/api/factories/" + factoryId + "/life-predictions?hours=72")
            .then(function (data) {
                lifePredictions = data.predictions || {};
                scheduleGridUpdate(devices);
            }).catch(function () { });
    }

    function loadDevices() {
        fetchJSON(API_BASE + "/api/devices").then(function (data) {
            devices = data;
            deviceMap = {};
            data.forEach(function (d) { deviceMap[d.device_id] = d; });
            populateFilters(data);
            if (!gridBuilt) {
                buildGrid(data);
                gridBuilt = true;
            } else {
                scheduleGridUpdate(data);
            }
        }).catch(function (err) {
            console.error("Failed to load devices:", err);
        });
    }

    function populateFilters(data) {
        var areas = {};
        var types = {};
        data.forEach(function (d) {
            if (d.area) areas[d.area] = true;
            if (d.equipment_type) types[d.equipment_type] = true;
        });
        var areaOptions = '<option value="">全部</option>';
        Object.keys(areas).sort().forEach(function (a) {
            areaOptions += '<option value="' + a + '">' + a + '</option>';
        });
        if (filterArea.innerHTML !== areaOptions) filterArea.innerHTML = areaOptions;

        var typeOptions = '<option value="">全部</option>';
        Object.keys(types).sort().forEach(function (t) {
            typeOptions += '<option value="' + t + '">' + t + '</option>';
        });
        if (filterType.innerHTML !== typeOptions) filterType.innerHTML = typeOptions;
    }

    function filterDevices() {
        var area = filterArea.value;
        var type = filterType.value;
        var health = filterHealth.value;

        return devices.filter(function (d) {
            if (area && d.area !== area) return false;
            if (type && d.equipment_type !== type) return false;
            if (health) {
                var s = d.health_score;
                if (health === "good" && (s == null || s < 80)) return false;
                if (health === "warn" && (s == null || s < 60 || s >= 80)) return false;
                if (health === "danger" && (s != null && s >= 60)) return false;
            }
            return true;
        });
    }

    function switchView(view, factoryId) {
        currentView = view;
        currentFactoryId = factoryId || null;

        document.querySelectorAll(".view-btn").forEach(function (btn) {
            btn.classList.toggle("active", btn.getAttribute("data-view") === view);
        });

        if (view === "factory") {
            factoryMapContainer.style.display = "block";
            deviceGridContainer.style.display = "none";
            backToFactoriesBtn.style.display = "none";
            loadFactories();
        } else {
            factoryMapContainer.style.display = "none";
            deviceGridContainer.style.display = "block";
            backToFactoriesBtn.style.display = factoryId ? "inline-block" : "none";

            if (factoryId) {
                loadFactoryDevices(factoryId);
                if (showLifePrediction) loadLifePredictions(factoryId);
            } else {
                loadDevices();
            }
        }
    }

    function renderFactoryMap(regionData) {
        factoryMapEl.innerHTML = "";
        var totalFactories = regionData.reduce(function (s, r) { return s + r.factory_count; }, 0);

        if (totalFactories > 20) {
            regionData.forEach(function (region) {
                var cluster = document.createElement("div");
                cluster.className = "factory-cluster";
                cluster.setAttribute("data-region", region.region);
                cluster.setAttribute("role", "button");
                cluster.setAttribute("tabindex", "0");

                var hc = healthClass(region.avg_health);
                cluster.innerHTML =
                    '<div class="cluster-header">' +
                    '<span class="cluster-icon">📍</span>' +
                    '<span class="cluster-name">' + region.region + '</span>' +
                    '<span class="cluster-count">(' + region.factory_count + '个厂区)</span>' +
                    '</div>' +
                    '<div class="cluster-stats">' +
                    '<span>设备总数: ' + region.total_devices + '</span>' +
                    '<span class="' + hc + '">平均健康: ' + region.avg_health + '</span>' +
                    '</div>' +
                    '<div class="cluster-expand">点击展开 →</div>';

                cluster.addEventListener("click", function () {
                    expandCluster(region, cluster);
                });

                factoryMapEl.appendChild(cluster);
            });
        } else {
            var allFactories = [];
            regionData.forEach(function (r) {
                allFactories = allFactories.concat(r.factories);
            });
            renderFactoryCards(allFactories, factoryMapEl);
        }
    }

    function expandCluster(region, clusterEl) {
        var parent = clusterEl.parentNode;
        var fragment = document.createDocumentFragment();
        renderFactoryCards(region.factories, fragment);
        parent.insertBefore(fragment, clusterEl);
        clusterEl.outerHTML = "";
    }

    function renderFactoryCards(factoryList, container) {
        factoryList.forEach(function (f) {
            var card = document.createElement("div");
            card.className = "factory-card";
            card.setAttribute("data-factory-id", f.factory_id);
            card.setAttribute("role", "button");
            card.setAttribute("tabindex", "0");

            var avgHealth = f.avg_health || 0;
            var hc = healthClass(avgHealth);

            card.innerHTML =
                '<div class="factory-header">' +
                '<span class="factory-icon">🏭</span>' +
                '<span class="factory-name">' + f.factory_name + '</span>' +
                '</div>' +
                '<div class="factory-stats">' +
                '<div class="factory-stat">' +
                '<span class="stat-label">设备总数</span>' +
                '<span class="stat-value">' + (f.device_count || 0) + '</span>' +
                '</div>' +
                '<div class="factory-stat">' +
                '<span class="stat-label">平均健康度</span>' +
                '<span class="stat-value ' + hc + '">' + (avgHealth != null ? avgHealth : "--") + '</span>' +
                '</div>' +
                '</div>' +
                '<div class="factory-address">' + (f.address || "") + '</div>' +
                '<div class="factory-enter">点击进入详情 →</div>';

            card.addEventListener("click", function () {
                switchView("devices", f.factory_id);
            });

            container.appendChild(card);
        });
    }

    function buildGrid(data) {
        var filtered = filterDevices();
        gridEl.innerHTML = "";
        filtered.forEach(function (d) {
            var cell = document.createElement("div");
            cell.setAttribute("data-device-id", d.device_id);
            cell.setAttribute("role", "button");
            cell.setAttribute("tabindex", "0");
            var titleText = d.device_name + " 评分:" + (d.health_score != null ? d.health_score : "--");
            var pred = lifePredictions[d.device_id];
            if (pred && pred.remaining_days != null) {
                titleText += " 剩余寿命:" + pred.remaining_days + "天";
            }
            cell.title = titleText;

            var cls = "device-cell " + healthBandClass(d.health_score);
            if (d.health_score != null && d.health_score < 60) cls += " alerting";
            if (d.status !== "online" && d.health_score == null) cls += " offline";
            if (d.device_id === selectedDeviceId) cls += " selected";
            cell.className = cls;

            var idSpan = document.createElement("span");
            idSpan.className = "device-id";
            idSpan.textContent = d.device_id;

            var scoreSpan = document.createElement("span");
            scoreSpan.className = "device-score";
            scoreSpan.textContent = d.health_score != null ? d.health_score : "--";

            cell.appendChild(idSpan);
            cell.appendChild(scoreSpan);

            if (showLifePrediction && pred && pred.remaining_days != null) {
                var lifeSpan = document.createElement("span");
                lifeSpan.className = "device-life";
                var days = pred.remaining_days;
                if (days < 7) lifeSpan.classList.add("life-critical");
                else if (days < 30) lifeSpan.classList.add("life-warn");
                lifeSpan.textContent = days.toFixed(0) + "天";
                cell.appendChild(lifeSpan);
            }

            gridEl.appendChild(cell);
        });
    }

    function scheduleGridUpdate(data) {
        pendingUpdates = data;
        if (!rafId) {
            rafId = requestAnimationFrame(applyGridUpdate);
        }
    }

    function applyGridUpdate() {
        rafId = null;
        if (!pendingUpdates) return;
        var data = pendingUpdates;
        pendingUpdates = null;

        var filtered = filterDevices();
        var filteredMap = {};
        filtered.forEach(function (d) { filteredMap[d.device_id] = d; });

        var existing = gridEl.querySelectorAll(".device-cell");
        var existingMap = {};
        existing.forEach(function (cell) {
            existingMap[cell.getAttribute("data-device-id")] = cell;
        });

        var fragment = document.createDocumentFragment();
        var orderedIds = filtered.map(function (d) { return d.device_id; });

        orderedIds.forEach(function (deviceId) {
            var d = filteredMap[deviceId];
            var cell = existingMap[deviceId];
            var pred = lifePredictions[deviceId];
            var titleText = d.device_name + " 评分:" + (d.health_score != null ? d.health_score : "--");
            if (pred && pred.remaining_days != null) {
                titleText += " 剩余寿命:" + pred.remaining_days + "天";
            }

            if (cell) {
                delete existingMap[deviceId];
                var cls = "device-cell " + healthBandClass(d.health_score);
                if (d.health_score != null && d.health_score < 60) cls += " alerting";
                if (d.status !== "online" && d.health_score == null) cls += " offline";
                if (d.device_id === selectedDeviceId) cls += " selected";
                cell.className = cls;
                cell.title = titleText;
                cell.querySelector(".device-score").textContent = d.health_score != null ? d.health_score : "--";

                var existingLife = cell.querySelector(".device-life");
                if (showLifePrediction && pred && pred.remaining_days != null) {
                    if (existingLife) {
                        existingLife.className = "device-life";
                        var days = pred.remaining_days;
                        if (days < 7) existingLife.classList.add("life-critical");
                        else if (days < 30) existingLife.classList.add("life-warn");
                        existingLife.textContent = days.toFixed(0) + "天";
                    } else {
                        var lifeSpan = document.createElement("span");
                        lifeSpan.className = "device-life";
                        var days = pred.remaining_days;
                        if (days < 7) lifeSpan.classList.add("life-critical");
                        else if (days < 30) lifeSpan.classList.add("life-warn");
                        lifeSpan.textContent = days.toFixed(0) + "天";
                        cell.appendChild(lifeSpan);
                    }
                } else if (existingLife) {
                    existingLife.remove();
                }

                fragment.appendChild(cell);
            } else {
                cell = document.createElement("div");
                cell.setAttribute("data-device-id", d.device_id);
                cell.setAttribute("role", "button");
                cell.setAttribute("tabindex", "0");
                cell.title = titleText;

                var cls = "device-cell " + healthBandClass(d.health_score);
                if (d.health_score != null && d.health_score < 60) cls += " alerting";
                if (d.status !== "online" && d.health_score == null) cls += " offline";
                if (d.device_id === selectedDeviceId) cls += " selected";
                cell.className = cls;

                var idSpan = document.createElement("span");
                idSpan.className = "device-id";
                idSpan.textContent = d.device_id;

                var scoreSpan = document.createElement("span");
                scoreSpan.className = "device-score";
                scoreSpan.textContent = d.health_score != null ? d.health_score : "--";

                cell.appendChild(idSpan);
                cell.appendChild(scoreSpan);

                if (showLifePrediction && pred && pred.remaining_days != null) {
                    var lifeSpan = document.createElement("span");
                    lifeSpan.className = "device-life";
                    var days = pred.remaining_days;
                    if (days < 7) lifeSpan.classList.add("life-critical");
                    else if (days < 30) lifeSpan.classList.add("life-warn");
                    lifeSpan.textContent = days.toFixed(0) + "天";
                    cell.appendChild(lifeSpan);
                }

                fragment.appendChild(cell);
            }
        });

        Object.keys(existingMap).forEach(function (id) {
            existingMap[id].remove();
        });

        gridEl.appendChild(fragment);
    }

    gridEl.addEventListener("click", function (e) {
        var cell = e.target.closest(".device-cell");
        if (cell) {
            var id = cell.getAttribute("data-device-id");
            openSidebar(id);
        }
    });

    function openSidebar(deviceId) {
        selectedDeviceId = deviceId;
        sidebarEl.classList.add("open");
        sidebarTitle.textContent = "加载中...";

        scheduleGridUpdate(devices);

        Promise.all([
            fetchJSON(API_BASE + "/api/devices/" + deviceId),
            fetchJSON(API_BASE + "/api/devices/" + deviceId + "/trend?hours=24"),
            fetchJSON(API_BASE + "/api/devices/" + deviceId + "/life-prediction?hours=72"),
            fetchJSON(API_BASE + "/api/spare-parts")
        ]).then(function (results) {
            var d = results[0];
            var trend = results[1];
            var lifePred = results[2];
            var spareParts = results[3];

            renderDeviceInfo(d);
            renderLifePrediction(lifePred);
            renderSpareParts(spareParts, d.equipment_type);
            renderTrendCharts(trend);
            return fetchJSON(API_BASE + "/api/work-orders?device_id=" + deviceId);
        }).then(function (orders) {
            renderWorkOrders(orders);
        }).catch(function (err) {
            console.error("Sidebar load error:", err);
        });
    }

    function renderLifePrediction(pred) {
        var container = document.getElementById("lifePrediction");
        if (!pred || pred.remaining_days == null || pred.status === "insufficient_data") {
            container.style.display = "none";
            return;
        }

        container.style.display = "block";
        var valueEl = document.getElementById("predictionValue");
        var detailEl = document.getElementById("predictionDetail");

        if (pred.status === "stable_or_improving") {
            valueEl.textContent = "> 365 天";
            valueEl.className = "prediction-value stable";
            detailEl.innerHTML = "设备健康状态稳定或改善，暂无失效风险";
        } else {
            var days = pred.remaining_days;
            valueEl.textContent = days.toFixed(1) + " 天";
            valueEl.className = "prediction-value";
            if (days < 7) valueEl.classList.add("critical");
            else if (days < 30) valueEl.classList.add("warn");

            detailEl.innerHTML =
                '<div>置信度：<span style="color:#fbbf24;">' + (pred.confidence * 100).toFixed(0) + '%</span></div>' +
                '<div>衰退速率：<span style="color:#f87171;">' + Math.abs(pred.slope).toFixed(2) + ' 分/小时</span></div>' +
                '<div>数据点数：' + pred.data_points + ' 个</div>' +
                '<div style="font-size:11px;color:#6b7280;margin-top:4px;">基于近72小时健康评分线性回归预测</div>';
        }
    }

    function renderSpareParts(allParts, equipmentType) {
        var container = document.getElementById("sparePartsInfo");
        var listEl = document.getElementById("sparePartsList");

        var relevantParts = allParts.filter(function (p) {
            return p.equipment_type === equipmentType;
        });

        if (!relevantParts.length) {
            container.style.display = "none";
            return;
        }

        container.style.display = "block";
        var html = "";
        relevantParts.forEach(function (p) {
            var lowStock = p.stock_quantity < p.safe_stock_level;
            var cls = lowStock ? "part-card low" : "part-card";
            html += '<div class="' + cls + '">' +
                '<div class="part-header">' +
                '<span class="part-name">' + p.part_name + '</span>' +
                '<span class="part-stock ' + (lowStock ? "low" : "") + '">' +
                p.stock_quantity + ' / ' + p.safe_stock_level +
                '</span>' +
                '</div>' +
                '<div class="part-detail">型号：' + p.part_id + ' | 供应商：' + p.supplier + ' | 单价：¥' + p.unit_price.toLocaleString() + '</div>' +
                (lowStock ? '<div class="part-warning">⚠️ 库存不足安全线</div>' : '') +
                '</div>';
        });
        listEl.innerHTML = html;
    }

    function closeSidebar() {
        selectedDeviceId = null;
        sidebarEl.classList.remove("open");
        scheduleGridUpdate(devices);
    }

    function renderDeviceInfo(d) {
        sidebarTitle.textContent = d.device_name || d.device_id;
        var hc = healthClass(d.health_score);
        var scoreHtml = d.health_score != null
            ? '<span class="health-badge ' + hc + '">' + d.health_score + '</span>'
            : "--";

        var html = '<table>' +
            '<tr><td>设备ID</td><td>' + d.device_id + '</td></tr>' +
            '<tr><td>设备名称</td><td>' + (d.device_name || "--") + '</td></tr>' +
            '<tr><td>设备类型</td><td>' + (d.equipment_type || "--") + '</td></tr>' +
            '<tr><td>所属区域</td><td>' + (d.area || "--") + '</td></tr>' +
            '<tr><td>健康评分</td><td>' + scoreHtml + '</td></tr>' +
            '<tr><td>当前温度</td><td>' + (d.temperature != null ? d.temperature.toFixed(1) + " °C" : "--") + '</td></tr>' +
            '<tr><td>当前振动</td><td>' + (d.vibration != null ? d.vibration.toFixed(3) + " mm/s" : "--") + '</td></tr>' +
            '<tr><td>RF功率</td><td>' + (d.rf_power != null ? d.rf_power.toFixed(1) + " W" : "--") + '</td></tr>' +
            '<tr><td>温度基线</td><td>' + (d.baseline_temperature != null ? d.baseline_temperature.toFixed(1) + " °C" : "--") + '</td></tr>' +
            '<tr><td>振动基线</td><td>' + (d.baseline_vibration != null ? d.baseline_vibration.toFixed(3) + " mm/s" : "--") + '</td></tr>' +
            '<tr><td>功率基线</td><td>' + (d.baseline_rf_power != null ? d.baseline_rf_power.toFixed(1) + " W" : "--") + '</td></tr>' +
            '<tr><td>负责工程师</td><td>' + (d.engineer_id || "--") + '</td></tr>' +
            '<tr><td>最后上报</td><td>' + formatTime(d.time) + '</td></tr>' +
            '</table>';

        document.getElementById("deviceInfo").innerHTML = html;
    }

    function renderTrendCharts(trend) {
        var baselines = trend.baselines || {};
        var data = trend.data || [];

        drawChart(
            "chartTemperature", data, "time", "temperature",
            "温度 (°C)", "#f97316", baselines.baseline_temperature
        );
        drawChart(
            "chartVibration", data, "time", "vibration",
            "振动 (mm/s)", "#a855f7", baselines.baseline_vibration
        );
        drawChart(
            "chartRfPower", data, "time", "rf_power",
            "RF功率 (W)", "#3b82f6", baselines.baseline_rf_power
        );
        drawChart(
            "chartHealth", data, "time", "health_score",
            "健康评分", "#22c55e", 60
        );
    }

    function drawChart(canvasId, data, xKey, yKey, label, color, baseline) {
        var canvas = document.getElementById(canvasId);
        if (!canvas) return;

        var dpr = window.devicePixelRatio || 1;
        var rect = canvas.parentElement.getBoundingClientRect();
        var w = rect.width - 16;
        var h = 160;

        canvas.width = w * dpr;
        canvas.height = h * dpr;
        canvas.style.width = w + "px";
        canvas.style.height = h + "px";

        var ctx = canvas.getContext("2d");
        ctx.scale(dpr, dpr);

        var padLeft = 55;
        var padRight = 10;
        var padTop = 24;
        var padBottom = 28;
        var chartW = w - padLeft - padRight;
        var chartH = h - padTop - padBottom;

        ctx.fillStyle = "#111827";
        ctx.fillRect(0, 0, w, h);

        ctx.font = "11px -apple-system, sans-serif";
        ctx.fillStyle = "#9ca3af";
        ctx.fillText(label, padLeft, 14);

        if (!data.length) {
            ctx.fillStyle = "#6b7280";
            ctx.textAlign = "center";
            ctx.fillText("暂无数据", w / 2, h / 2);
            ctx.textAlign = "start";
            return;
        }

        var values = data.map(function (d) { return d[yKey]; }).filter(function (v) { return v != null; });
        if (!values.length) return;

        var minVal = Math.min.apply(null, values);
        var maxVal = Math.max.apply(null, values);
        if (baseline != null) {
            minVal = Math.min(minVal, baseline * 0.8);
            maxVal = Math.max(maxVal, baseline * 1.2);
        }
        var range = maxVal - minVal;
        if (range < 0.001) { range = 1; minVal = minVal - 0.5; }
        minVal -= range * 0.1;
        maxVal += range * 0.1;

        function xPos(i) {
            return padLeft + (i / (data.length - 1)) * chartW;
        }
        function yPos(v) {
            return padTop + chartH - ((v - minVal) / (maxVal - minVal)) * chartH;
        }

        ctx.strokeStyle = "#1e293b";
        ctx.lineWidth = 1;
        for (var i = 0; i <= 4; i++) {
            var yy = padTop + (i / 4) * chartH;
            ctx.beginPath();
            ctx.moveTo(padLeft, yy);
            ctx.lineTo(padLeft + chartW, yy);
            ctx.stroke();

            var val = maxVal - (i / 4) * (maxVal - minVal);
            ctx.fillStyle = "#6b7280";
            ctx.font = "10px monospace";
            ctx.textAlign = "right";
            ctx.fillText(val.toFixed(1), padLeft - 6, yy + 3);
        }

        for (var i = 0; i < data.length; i += Math.max(1, Math.floor(data.length / 6))) {
            var xx = xPos(i);
            ctx.beginPath();
            ctx.strokeStyle = "#1e293b";
            ctx.moveTo(xx, padTop);
            ctx.lineTo(xx, padTop + chartH);
            ctx.stroke();

            var t = new Date(data[i][xKey]);
            ctx.fillStyle = "#6b7280";
            ctx.font = "9px monospace";
            ctx.textAlign = "center";
            ctx.fillText(
                t.getHours().toString().padStart(2, "0") + ":" + t.getMinutes().toString().padStart(2, "0"),
                xx, h - 6
            );
        }

        if (baseline != null) {
            var by = yPos(baseline);
            ctx.setLineDash([4, 4]);
            ctx.strokeStyle = "rgba(255,255,255,0.3)";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(padLeft, by);
            ctx.lineTo(padLeft + chartW, by);
            ctx.stroke();
            ctx.setLineDash([]);

            ctx.fillStyle = "rgba(255,255,255,0.5)";
            ctx.font = "9px sans-serif";
            ctx.textAlign = "left";
            ctx.fillText("基线", padLeft + 4, by - 4);
        }

        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        var started = false;
        for (var i = 0; i < data.length; i++) {
            var v = data[i][yKey];
            if (v == null) continue;
            var x = xPos(i);
            var y = yPos(v);
            if (!started) {
                ctx.moveTo(x, y);
                started = true;
            } else {
                ctx.lineTo(x, y);
            }
        }
        ctx.stroke();

        if (started && data.length > 1) {
            var lastValidIdx = data.length - 1;
            while (lastValidIdx >= 0 && data[lastValidIdx][yKey] == null) lastValidIdx--;
            if (lastValidIdx >= 0) {
                ctx.lineTo(xPos(lastValidIdx), padTop + chartH);
                ctx.lineTo(xPos(0), padTop + chartH);
                ctx.closePath();
                var grad = ctx.createLinearGradient(0, padTop, 0, padTop + chartH);
                var r = parseInt(color.slice(1, 3), 16);
                var g = parseInt(color.slice(3, 5), 16);
                var b = parseInt(color.slice(5, 7), 16);
                grad.addColorStop(0, "rgba(" + r + "," + g + "," + b + ",0.25)");
                grad.addColorStop(1, "rgba(" + r + "," + g + "," + b + ",0)");
                ctx.fillStyle = grad;
                ctx.fill();
            }
        }

        ctx.textAlign = "start";
    }

    function renderWorkOrders(orders) {
        var container = document.getElementById("deviceWorkOrders");
        if (!orders.length) {
            container.innerHTML = "<h3>维保工单</h3><p style='color:#6b7280;font-size:13px;'>暂无工单</p>";
            return;
        }

        var statusLabels = {
            pending: "待接单",
            accepted: "处理中",
            completed: "已完成",
            verified: "已验收"
        };

        var html = "<h3>维保工单</h3>";
        orders.forEach(function (wo) {
            var actionsHtml = "";
            if (wo.status === "pending") {
                actionsHtml = '<button class="wo-btn accept" data-order-id="' + wo.id + '" data-action="accept">接单</button>';
                actionsHtml += '<button class="wo-btn recommend" data-order-id="' + wo.id + '" data-action="recommend">智能推荐</button>';
            } else if (wo.status === "accepted") {
                actionsHtml = '<button class="wo-btn complete" data-order-id="' + wo.id + '" data-action="complete">完成</button>';
            } else if (wo.status === "completed") {
                actionsHtml = '<button class="wo-btn verify" data-order-id="' + wo.id + '" data-action="verify">验收</button>';
            }

            var extraHtml = "";
            if (wo.recommended_engineer_id) {
                extraHtml += '<div class="wo-recommend">' +
                    '<span class="recommend-label">🤖 推荐工程师：</span>' +
                    '<span class="recommend-engineer">' + wo.recommended_engineer_id + '</span>' +
                    '</div>';
            }
            if (wo.assignment_reason) {
                extraHtml += '<div class="wo-reason-detail">' + wo.assignment_reason + '</div>';
            }
            if (wo.spare_part_id) {
                extraHtml += '<div style="font-size:12px;color:#60a5fa;">备件：' + wo.spare_part_id + '</div>';
            }

            html += '<div class="wo-card ' + wo.status + '">' +
                '<div class="wo-header">' +
                '<span class="wo-id">工单 #' + wo.id + '</span>' +
                '<span class="wo-status ' + wo.status + '">' + (statusLabels[wo.status] || wo.status) + '</span>' +
                '</div>' +
                '<div class="wo-reason">' + (wo.reason || "") + '</div>' +
                extraHtml +
                (wo.engineer_name ? '<div style="font-size:12px;color:#9ca3af;margin-bottom:4px;">工程师：' + wo.engineer_name + '</div>' : '') +
                '<div class="wo-actions">' + actionsHtml + '</div>' +
                '</div>';
        });

        container.innerHTML = html;

        container.querySelectorAll(".wo-btn").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var orderId = this.getAttribute("data-order-id");
                var action = this.getAttribute("data-action");

                if (action === "recommend") {
                    fetchJSON(API_BASE + "/api/work-orders/" + orderId + "/recommend-engineer")
                        .then(function (data) {
                            if (data.recommended_engineer_id) {
                                alert("智能推荐：" + data.recommended_engineer_id +
                                    "\n\n理由：" + data.assignment_reason +
                                    "\n\n点击确定后将自动分配推荐工程师");
                                if (confirm("确定分配给 " + data.recommended_engineer_id + "？")) {
                                    postJSON(API_BASE + "/api/work-orders/" + orderId + "/accept").then(function () {
                                        openSidebar(selectedDeviceId);
                                    });
                                }
                            } else {
                                alert("暂无合适的工程师推荐：" + (data.assignment_reason || ""));
                            }
                        }).catch(function (err) {
                            alert("获取推荐失败: " + err.message);
                        });
                } else {
                    var url = API_BASE + "/api/work-orders/" + orderId + "/" + action;
                    postJSON(url).then(function () {
                        openSidebar(selectedDeviceId);
                    }).catch(function (err) {
                        alert("操作失败: " + err.message);
                    });
                }
            });
        });
    }

    function openPurchaseModal() {
        poModal.style.display = "flex";
        loadPurchaseOrders();
    }

    function closePurchaseModal() {
        poModal.style.display = "none";
    }

    function loadPurchaseOrders(status) {
        var url = API_BASE + "/api/purchase-orders";
        if (status) url += "?status=" + status;

        fetchJSON(url).then(function (pos) {
            renderPurchaseOrders(pos);
        }).catch(function () { });
    }

    function renderPurchaseOrders(pos) {
        var listEl = document.getElementById("poList");
        if (!pos.length) {
            listEl.innerHTML = '<p style="color:#6b7280;text-align:center;padding:40px;">暂无采购申请</p>';
            return;
        }

        var statusLabels = {
            pending: "待审批",
            approved: "已审批",
            ordered: "已下单",
            received: "已收货"
        };

        var html = "";
        pos.forEach(function (po) {
            var actionsHtml = "";
            if (po.status === "pending") {
                actionsHtml = '<button class="po-btn approve" data-po-id="' + po.id + '">审批</button>';
            } else if (po.status === "approved") {
                actionsHtml = '<button class="po-btn receive" data-po-id="' + po.id + '">收货</button>';
            }

            html += '<div class="po-card ' + po.status + '">' +
                '<div class="po-header">' +
                '<span class="po-id">采购申请 #' + po.id + '</span>' +
                '<span class="po-status ' + po.status + '">' + (statusLabels[po.status] || po.status) + '</span>' +
                '</div>' +
                '<div class="po-content">' +
                '<div><strong>' + po.part_name + '</strong> (' + po.part_id + ')</div>' +
                '<div>数量：' + po.quantity + ' | 设备类型：' + po.equipment_type + '</div>' +
                '<div style="font-size:12px;color:#9ca3af;margin-top:4px;">' + (po.reason || "") + '</div>' +
                '<div style="font-size:12px;color:#6b7280;margin-top:2px;">创建时间：' + formatTime(po.created_at) + '</div>' +
                '</div>' +
                '<div class="po-actions">' + actionsHtml + '</div>' +
                '</div>';
        });

        listEl.innerHTML = html;

        listEl.querySelectorAll(".po-btn.approve").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var poId = parseInt(this.getAttribute("data-po-id"));
                postJSON(API_BASE + "/api/purchase-orders/" + poId + "/approve").then(function () {
                    loadPurchaseOrders(document.getElementById("poStatusFilter").value);
                }).catch(function (err) {
                    alert("审批失败: " + err.message);
                });
            });
        });

        listEl.querySelectorAll(".po-btn.receive").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var poId = parseInt(this.getAttribute("data-po-id"));
                postJSON(API_BASE + "/api/purchase-orders/" + poId + "/receive").then(function () {
                    loadPurchaseOrders(document.getElementById("poStatusFilter").value);
                }).catch(function (err) {
                    alert("收货失败: " + err.message);
                });
            });
        });
    }

    function showAlertPopup(alertData) {
        var typeMap = {
            overheat: "过热告警",
            vibration: "振动告警",
            work_order: "维保工单",
            purchase_order: "采购申请",
        };
        var alertType = alertData.alert_type || alertData.type || "alert";
        var title = typeMap[alertType] || "系统通知";
        var msg = alertData.message || JSON.stringify(alertData);
        var now = new Date().toLocaleString("zh-CN");

        var popupCls = "alert-popup";
        if (alertType === "vibration") popupCls += " alert-vibration";
        if (alertType === "work_order") popupCls += " alert-work-order";
        if (alertType === "purchase_order") popupCls += " alert-purchase-order";

        var popup = document.createElement("div");
        popup.className = popupCls;

        var extraHtml = "";
        if (alertType === "work_order" && alertData.spare_part_warning) {
            extraHtml += '<div style="font-size:12px;color:#fbbf24;margin-top:6px;">⚠️ ' + alertData.spare_part_warning + '</div>';
        }
        if (alertType === "work_order" && alertData.recommended_engineer_id) {
            extraHtml += '<div style="font-size:12px;color:#60a5fa;margin-top:4px;">🤖 推荐工程师：' + alertData.recommended_engineer_id + '</div>';
        }
        if (alertType === "purchase_order") {
            extraHtml += '<button class="alert-action-btn" style="margin-top:8px;width:100%;">查看采购申请</button>';
        }

        popup.innerHTML =
            '<div class="alert-popup-header">' +
            '<span class="alert-popup-type">' + title + '</span>' +
            '<button class="alert-popup-close">\u2715</button>' +
            '</div>' +
            '<div class="alert-popup-msg">' + msg + '</div>' +
            extraHtml +
            '<div class="alert-popup-time">' + now + '</div>';

        popup.querySelector(".alert-popup-close").addEventListener("click", function () {
            popup.remove();
        });

        var actionBtn = popup.querySelector(".alert-action-btn");
        if (actionBtn) {
            actionBtn.addEventListener("click", function () {
                openPurchaseModal();
                popup.remove();
            });
        }

        alertContainer.appendChild(popup);

        setTimeout(function () {
            if (popup.parentElement) popup.remove();
        }, 15000);

        while (alertContainer.children.length > 8) {
            alertContainer.removeChild(alertContainer.firstChild);
        }
    }

    function connectWebSocket() {
        var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        var url = protocol + "//" + window.location.host + "/ws/alerts";

        ws = new WebSocket(url);

        ws.onopen = function () {
            console.log("WebSocket connected");
        };

        ws.onmessage = function (event) {
            try {
                var data = JSON.parse(event.data);
                showAlertPopup(data);
                loadSummary();
            } catch (e) {
                console.error("WS message parse error:", e);
            }
        };

        ws.onclose = function () {
            console.log("WebSocket closed, reconnecting in 3s...");
            setTimeout(connectWebSocket, 3000);
        };

        ws.onerror = function () {
            ws.close();
        };
    }

    document.getElementById("sidebarClose").addEventListener("click", closeSidebar);

    filterArea.addEventListener("change", function () {
        buildGrid(devices);
    });
    filterType.addEventListener("change", function () {
        buildGrid(devices);
    });
    filterHealth.addEventListener("change", function () {
        buildGrid(devices);
    });

    showLifePredictionCheckbox.addEventListener("change", function () {
        showLifePrediction = this.checked;
        scheduleGridUpdate(devices);
    });

    document.querySelectorAll(".view-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            var view = this.getAttribute("data-view");
            switchView(view, null);
        });
    });

    backToFactoriesBtn.addEventListener("click", function () {
        switchView("factory", null);
    });

    document.getElementById("modalClose").addEventListener("click", closePurchaseModal);
    document.getElementById("openPurchaseBtn").addEventListener("click", openPurchaseModal);

    document.getElementById("poStatusFilter").addEventListener("change", function () {
        loadPurchaseOrders(this.value);
    });

    poModal.addEventListener("click", function (e) {
        if (e.target === poModal) closePurchaseModal();
    });

    function init() {
        _ensureHealthStyles();
        updateClock();
        setInterval(updateClock, 1000);

        loadFactories();
        loadSummary();
        connectWebSocket();

        refreshTimer = setInterval(function () {
            if (document.hidden) return;
            if (currentView === "factory") {
                loadFactories();
            } else {
                if (currentFactoryId) {
                    loadFactoryDevices(currentFactoryId);
                    if (showLifePrediction) loadLifePredictions(currentFactoryId);
                } else {
                    loadDevices();
                }
            }
            loadSummary();
            if (selectedDeviceId) {
                openSidebar(selectedDeviceId);
            }
        }, 30000);
    }

    init();
})();
