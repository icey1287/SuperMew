const { createApp } = Vue;

createApp({
    data() {
        return {
            messages: [],
            userInput: '',
            isLoading: false,
            activeNav: 'newChat',
            API_URL: '/chat',
            abortController: null,
            userId: 'user_' + Math.random().toString(36).substring(2, 11),
            sessionId: 'session_' + Date.now(),
            sessions: [],
            showHistorySidebar: false,
            isComposing: false,
            // 文档管理相关
            documents: [],
            documentsLoading: false,
            selectedFile: null,
            isUploading: false,
            uploadProgress: '',
            showTooltip: false,
            tooltipText: '',
            tooltipX: 0,
            tooltipY: 0,
            // 个人档案相关
            userProfile: null,
            selectedProfileFile: null,
            isUploadingProfile: false,
            profileProgress: '',
            /** 病历夹：先选医嘱项目，再选报告时间 */
            profileFilterOrder: '',
            profileFilterDate: '',
            showChartModal: false,
            /** 病历解析完成后的校对弹窗 */
            showProfileReviewModal: false,
            profileReviewDraft: null,
            /** 校对弹窗正在编辑的 records[].id */
            profileReviewRecordId: null,
            isSavingProfileReview: false,
            isDeletingMedicalRecord: false,
            /** 出院报告上传 */
            selectedDischargeFile: null,
            isUploadingDischarge: false,
            dischargeProgress: '',
            isDeletingDischargeReport: false,
            /** 复诊日历（展示出院医嘱中的 visit_date） */
            followUpCalendarYear: new Date().getFullYear(),
            followUpCalendarMonth: new Date().getMonth() + 1,
            /** 复诊日历：详情气泡（纯新增交互） */
            followUpBubbleVisible: false,
            followUpBubbleEvents: [],
            followUpBubbleDateKey: '',
            followUpBubblePos: { left: 0, top: 0 },
            showDictionaryModal: false,
            /** NCI 词典：直接调 glossary API（iframe 内嵌 widget 会先走 Adobe Analytics，易被拦截导致 Search 无反应） */
            nciDictionaryQuery: '',
            nciDictionaryResults: [],
            nciDictionaryTotal: 0,
            nciDictionaryLoading: false,
            nciDictionaryError: '',
            nciDictionarySearched: false,
            /** NCI API：Begins=前缀，Contains=包含 */
            nciDictionaryMatchType: 'Begins',
            selectedChartIndicator: '',
            chartInstance: null,
            // 提问优化
            optimizedQuestions: [],
            isOptimizing: false,
            // 思考模式
            thinkMode: 'normal'
        };
    },
    computed: {
        /** 医嘱项目下拉：去重 */
        orderCategoryOptions() {
            const recs = this.userProfile?.records;
            if (!recs || !recs.length) return [];
            return [...new Set(recs.map((r) => (r.order_category || '').trim()).filter(Boolean))].sort();
        },
        /** 当前医嘱项目下的报告日期 */
        reportDateOptionsForFilter() {
            const recs = this.userProfile?.records || [];
            const pool = this.profileFilterOrder
                ? recs.filter((r) => (r.order_category || '') === this.profileFilterOrder)
                : recs;
            return [...new Set(pool.map((r) => (r.report_date || '').trim()).filter(Boolean))].sort();
        },
        /** 当前筛选下展示的这一份病历 */
        activeMedicalRecord() {
            const recs = this.userProfile?.records || [];
            if (!recs.length) return null;
            let pool = this.profileFilterOrder
                ? recs.filter((r) => (r.order_category || '') === this.profileFilterOrder)
                : recs;
            if (!pool.length) pool = recs;
            if (this.profileFilterDate) {
                const exact = pool.find((r) => (r.report_date || '') === this.profileFilterDate);
                if (exact) return exact;
            }
            return pool[pool.length - 1];
        },
        /** 趋势图：同医嘱项目下跨时间对比指标 */
        uniqueTestItemNames() {
            const recs = this.userProfile?.records || [];
            const pool = this.profileFilterOrder
                ? recs.filter((r) => (r.order_category || '') === this.profileFilterOrder)
                : recs;
            const names = pool.flatMap((r) => (r.test_items || []).map((item) => item.item_name));
            return [...new Set(names)].filter(Boolean);
        },
        /** 出院随访日期 -> 事项列表（用于日历打点） */
        followUpEventsByDate() {
            const map = Object.create(null);
            for (const dr of this.userProfile?.discharge_reports || []) {
                const src = dr.source_filename || '出院报告';
                for (const it of dr.follow_up_items || []) {
                    let vd = (it.visit_date || '').trim().replace(/\//g, '-');
                    if (!vd) continue;
                    if (vd.length >= 10) vd = vd.slice(0, 10);
                    if (!/^\d{4}-\d{2}-\d{2}$/.test(vd)) continue;
                    if (!map[vd]) map[vd] = [];
                    map[vd].push({
                        title: (it.item_title || '复诊').trim() || '复诊提醒',
                        detail: (it.detail || '').trim(),
                        source: src,
                    });
                }
            }
            return map;
        },
        /** 当前月的周行，每格 { day: 1..31 | null } */
        followUpCalendarWeeks() {
            const y = this.followUpCalendarYear;
            const m = this.followUpCalendarMonth;
            const first = new Date(y, m - 1, 1);
            const lastDate = new Date(y, m, 0).getDate();
            const startPad = first.getDay();
            const cells = [];
            for (let i = 0; i < startPad; i++) cells.push({ day: null });
            for (let d = 1; d <= lastDate; d++) cells.push({ day: d });
            while (cells.length % 7 !== 0) cells.push({ day: null });
            const weeks = [];
            for (let i = 0; i < cells.length; i += 7) {
                weeks.push(cells.slice(i, i + 7));
            }
            return weeks;
        },
        followUpBubbleDateLabel() {
            if (!this.followUpBubbleDateKey) return '';
            const parts = this.followUpBubbleDateKey.split('-');
            if (parts.length !== 3) return this.followUpBubbleDateKey;
            const [y, m, d] = parts;
            return `${y} 年 ${Number(m)} 月 ${Number(d)} 日`;
        },
        followUpBubbleBoxStyle() {
            if (!this.followUpBubbleVisible) return {};
            return {
                left: `${this.followUpBubblePos.left}px`,
                top: `${this.followUpBubblePos.top}px`,
            };
        },
    },
    mounted() {
        this.configureMarked();
        // 尝试从 localStorage 恢复用户ID
        const savedUserId = localStorage.getItem('userId');
        if (savedUserId) {
            this.userId = savedUserId;
        } else {
            localStorage.setItem('userId', this.userId);
        }
        this.loadProfile();
        
        // 事件委托处理点击引用跳转
        if (this.$refs.chatContainer) {
            this.$refs.chatContainer.addEventListener('click', (e) => {
                const citeRef = e.target.closest('.cite-ref');
                if (citeRef) {
                    const msgIndex = citeRef.getAttribute('data-msg-index');
                    const chunkIndex = citeRef.getAttribute('data-chunk-index');
                    if (msgIndex != null && chunkIndex != null) {
                        this.scrollToChunk(msgIndex, chunkIndex);
                    }
                }
            });
        }
        
        // 处理全局的名词解释气泡点击
        document.addEventListener('click', (e) => {
            const conceptRef = e.target.closest('.concept-tooltip');
            if (conceptRef) {
                const desc = conceptRef.getAttribute('data-desc');
                if (desc) {
                    this.tooltipText = desc;
                    this.showTooltip = true;
                    this.$nextTick(() => {
                        const tooltip = this.$refs.globalTooltip;
                        if (tooltip) {
                            const rect = conceptRef.getBoundingClientRect();
                            let left = rect.left + (rect.width / 2) - (tooltip.offsetWidth / 2);
                            let top = rect.top - tooltip.offsetHeight - 8;
                            
                            // 边界检测
                            if (left < 10) left = 10;
                            if (top < 10) top = rect.bottom + 8;
                            
                            this.tooltipX = left;
                            this.tooltipY = top;
                        }
                    });
                }
            } else {
                this.showTooltip = false;
            }
        });

        this._followUpBubbleEscHandler = (e) => {
            if (e.key === 'Escape' && this.followUpBubbleVisible) {
                e.preventDefault();
                this.closeFollowUpBubble();
            }
        };
        document.addEventListener('keydown', this._followUpBubbleEscHandler);
    },
    beforeUnmount() {
        if (this._followUpBubbleEscHandler) {
            document.removeEventListener('keydown', this._followUpBubbleEscHandler);
        }
    },
    methods: {
        configureMarked() {
            marked.setOptions({
                highlight: function(code, lang) {
                    const language = hljs.getLanguage(lang) ? lang : 'plaintext';
                    return hljs.highlight(code, { language }).value;
                },
                langPrefix: 'hljs language-',
                breaks: true,
                gfm: true
            });
        },
        
        parseMarkdown(text, msgIndex) {
            let html = marked.parse(text);
            let inCode = false;
            return html.split(/(<[^>]*>)/).map(part => {
                if (part.startsWith('<')) {
                    if (part.startsWith('<code') || part.startsWith('<pre')) inCode = true;
                    if (part.startsWith('</code') || part.startsWith('</pre')) inCode = false;
                    return part;
                }
                if (!inCode) {
                    return part.replace(/\[([\d\s,]+)\]/g, (match, p1) => {
                        const numbers = p1.split(',').map(n => n.trim()).filter(n => /^\d+$/.test(n));
                        if (numbers.length === 0) return match;
                        return numbers.map(n => `<sup class="cite-ref" data-msg-index="${msgIndex}" data-chunk-index="${n}">[${n}]</sup>`).join('');
                    });
                }
                return part;
            }).join('');
        },
        
        scrollToChunk(msgIndex, chunkIndex) {
            const msgEl = document.querySelectorAll('.message')[msgIndex];
            if (msgEl) {
                const details = msgEl.querySelector('details.references-details');
                if (details) {
                    details.open = true;
                }
                const chunkEl = document.getElementById(`chunk-${msgIndex}-${chunkIndex}`);
                if (chunkEl) {
                    chunkEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    chunkEl.classList.add('highlight-chunk');
                    setTimeout(() => {
                        chunkEl.classList.remove('highlight-chunk');
                    }, 2000);
                }
            }
        },

        async copyText(text, event) {
            const icon = event.currentTarget.querySelector('i');
            const oldClass = icon.className;
            
            const successCallback = () => {
                icon.className = 'fas fa-check';
                setTimeout(() => {
                    icon.className = oldClass;
                }, 2000);
            };

            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(text);
                    successCallback();
                } else {
                    // Fallback for non-secure contexts (HTTP over network)
                    const textArea = document.createElement("textarea");
                    textArea.value = text;
                    textArea.style.position = "fixed";
                    textArea.style.left = "-999999px";
                    document.body.appendChild(textArea);
                    textArea.focus();
                    textArea.select();
                    document.execCommand('copy');
                    textArea.remove();
                    successCallback();
                }
            } catch (err) {
                console.error('Failed to copy text: ', err);
                alert("复制失败，您的浏览器不支持该功能。");
            }
        },

        toggleAction(event, type) {
            const icon = event.currentTarget.querySelector('i');
            if (type === 'up') {
                icon.className = icon.className.includes('fas') ? 'far fa-thumbs-up' : 'fas fa-thumbs-up';
            } else if (type === 'down') {
                icon.className = icon.className.includes('fas') ? 'far fa-thumbs-down' : 'fas fa-thumbs-down';
            } else if (type === 'share') {
                const oldClass = icon.className;
                icon.className = 'fas fa-check';
                setTimeout(() => { icon.className = oldClass; }, 1000);
            } else {
                // Default for star
                icon.className = icon.className.includes('fas') ? 'far fa-star' : 'fas fa-star';
            }
        },

        sendFollowUp(question) {
            this.userInput = question;
            this.handleSend();
        },

        openDictionaryModal() {
            this.nciDictionaryError = '';
            this.showDictionaryModal = true;
        },

        closeDictionaryModal() {
            this.showDictionaryModal = false;
        },

        async runNciDictionarySearch() {
            const q = (this.nciDictionaryQuery || '').trim();
            if (!q) {
                this.nciDictionaryError = '请输入要查询的术语。';
                return;
            }
            this.nciDictionaryLoading = true;
            this.nciDictionaryError = '';
            this.nciDictionarySearched = true;
            const encoded = encodeURIComponent(q);
            const match = this.nciDictionaryMatchType === 'Contains' ? 'Contains' : 'Begins';
            const url = `https://webapis.cancer.gov/glossary/v1/Terms/search/Cancer.gov/Patient/en/${encoded}?matchType=${match}&from=0&size=50`;
            try {
                const res = await fetch(url);
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                this.nciDictionaryTotal = (data.meta && data.meta.totalResults) || 0;
                this.nciDictionaryResults = Array.isArray(data.results) ? data.results : [];
            } catch (e) {
                console.error('NCI dictionary search failed', e);
                this.nciDictionaryResults = [];
                this.nciDictionaryTotal = 0;
                this.nciDictionaryError = '查询失败，请检查网络或稍后重试。（术语以英文为主）';
            } finally {
                this.nciDictionaryLoading = false;
            }
        },

        async handleOptimize() {
            const text = this.userInput.trim();
            if (!text || this.isOptimizing) return;
            this.isOptimizing = true;
            try {
                const res = await fetch('/chat/optimize', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: text })
                });
                if (res.ok) {
                    const data = await res.json();
                    if (data.questions && data.questions.length > 0) {
                        this.optimizedQuestions = data.questions;
                    } else {
                        alert('未能生成优化提问，请尝试修改输入。');
                    }
                } else {
                    alert('优化提问请求失败，请稍后再试。');
                }
            } catch (e) {
                console.error(e);
            } finally {
                this.isOptimizing = false;
            }
        },

        applyOptimizedQuestion(q) {
            this.userInput = q;
            this.optimizedQuestions = [];
            this.$nextTick(() => {
                if (this.$refs.textarea) {
                    this.$refs.textarea.focus();
                    this.autoResize({ target: this.$refs.textarea });
                }
            });
        },

        escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        },
        
        handleCompositionStart() {
            this.isComposing = true;
        },
        
        handleCompositionEnd() {
            this.isComposing = false;
        },
        
        handleKeyDown(event) {
            // 如果是回车键且不是Shift+回车，且不在输入法组合中
            if (event.key === 'Enter' && !event.shiftKey && !this.isComposing) {
                event.preventDefault();
                this.handleSend();
            }
        },
        
        handleStop() {
            if (this.abortController) {
                this.abortController.abort();
            }
        },
        
        async handleSend() {
            const text = this.userInput.trim();
            if (!text || this.isLoading || this.isComposing) return;
            
            this.optimizedQuestions = []; // 发送前清空优化建议
            
            // Add user message
            this.messages.push({
                text: text,
                isUser: true,
                isComplete: true
            });
            
            // 立即将新对话添加到侧边栏，形成秒出卡片的体验
            if (this.messages.length === 1) {
                const tempTitle = text.length > 10 ? text.substring(0, 10) + '...' : text;
                const existingSession = this.sessions.find(s => s.session_id === this.sessionId);
                if (!existingSession) {
                    this.sessions.unshift({
                        session_id: this.sessionId,
                        title: tempTitle,
                        message_count: 1,
                        updated_at: new Date().toISOString()
                    });
                }
            }
            
            this.userInput = '';
            this.$nextTick(() => {
                this.resetTextareaHeight();
                this.scrollToBottom();
            });

            // Show loading
            this.isLoading = true;

            // 立刻创建气泡，显示思考动画（二合一：思考 + 流式输出在同一个气泡）
            this.messages.push({ 
                text: '', 
                isUser: false, 
                isThinking: true,
                isComplete: false,
                ragTrace: null,
                ragSteps: [],
                followUps: []
            });
            const botMsgIdx = this.messages.length - 1;

            // 用于终止请求
            this.abortController = new AbortController();

            try {
                const response = await fetch('/chat/stream', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        message: text,
                        user_id: this.userId,
                        session_id: this.sessionId,
                        think_mode: this.thinkMode
                    }),
                    signal: this.abortController.signal,
                });

                if (!response.ok) throw new Error(`HTTP ${response.status}`);

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                
                let buffer = '';
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    
                    buffer += decoder.decode(value, { stream: true });
                    
                    let eventEndIndex;
                    while ((eventEndIndex = buffer.indexOf('\n\n')) !== -1) {
                        const eventStr = buffer.slice(0, eventEndIndex);
                        buffer = buffer.slice(eventEndIndex + 2);
                        
                        if (eventStr.startsWith('data: ')) {
                            const dataStr = eventStr.slice(6);
                            if (dataStr === '[DONE]') continue;
                            try {
                                const data = JSON.parse(dataStr);
                                if (data.type === 'content') {
                                    // 收到第一个 token 时关闭思考动画
                                    if (this.messages[botMsgIdx].isThinking) {
                                        this.messages[botMsgIdx].isThinking = false;
                                    }
                                    this.messages[botMsgIdx].text += data.content;
                                } else if (data.type === 'trace') {
                                    this.messages[botMsgIdx].ragTrace = data.rag_trace;
                                } else if (data.type === 'rag_step') {
                                    // 实时 RAG 检索步骤 — 直接显示在思考气泡内
                                    if (!this.messages[botMsgIdx].ragSteps) {
                                        this.messages[botMsgIdx].ragSteps = [];
                                    }
                                    this.messages[botMsgIdx].ragSteps.push(data.step);
                                } else if (data.type === 'follow_ups') {
                                    this.messages[botMsgIdx].followUps = data.questions;
                                } else if (data.type === 'session_title') {
                                    // 乐观更新侧边栏标题（因为此时后端可能还没写入文件，直接请求会导致拉取不到数据）
                                    const s = this.sessions.find(s => s.session_id === data.session_id);
                                    if (s) {
                                        s.title = data.title;
                                        s.updated_at = new Date().toISOString();
                                        s.message_count = this.messages.length;
                                    } else {
                                        // 直接前置插入到本地历史记录数组中，实现“秒出”
                                        this.sessions.unshift({
                                            session_id: data.session_id,
                                            title: data.title,
                                            message_count: this.messages.length,
                                            updated_at: new Date().toISOString()
                                        });
                                    }
                                } else if (data.type === 'error') {
                                    this.messages[botMsgIdx].isThinking = false;
                                    this.messages[botMsgIdx].text += `\n[Error: ${data.content}]`;
                                }
                            } catch (e) {
                                console.warn('SSE parse error:', e);
                            }
                        }
                    }
                    this.$nextTick(() => this.scrollToBottom());
                }

            } catch (error) {
                if (error.name === 'AbortError') {
                    // 用户主动终止
                    this.messages[botMsgIdx].isThinking = false;
                    if (!this.messages[botMsgIdx].text) {
                        this.messages[botMsgIdx].text = '(已终止回答)';
                    } else {
                        this.messages[botMsgIdx].text += '\n\n_(回答已被终止)_';
                    }
                } else {
                    console.error('Error:', error);
                    this.messages[botMsgIdx].isThinking = false;
                    this.messages[botMsgIdx].text = `喵呜... 出了点问题：${error.message}`;
                }
            } finally {
                this.isLoading = false;
                this.abortController = null;
                if (this.messages[botMsgIdx]) {
                    this.messages[botMsgIdx].isComplete = true;
                }
                this.$nextTick(() => this.scrollToBottom());
            }
        },
        
        autoResize(event) {
            const textarea = event.target;
            textarea.style.height = 'auto';
            textarea.style.height = textarea.scrollHeight + 'px';
        },
        
        resetTextareaHeight() {
            if (this.$refs.textarea) {
                this.$refs.textarea.style.height = 'auto';
            }
        },
        
        scrollToBottom() {
            if (this.$refs.chatContainer) {
                this.$refs.chatContainer.scrollTop = this.$refs.chatContainer.scrollHeight;
            }
        },
        
        handleNewChat() {
            this.messages = [];
            this.sessionId = 'session_' + Date.now();
            this.activeNav = 'newChat';
            this.showHistorySidebar = false;
        },
        
        handleClearChat() {
            if (confirm('确定要清空当前对话吗？喵？')) {
                this.messages = [];
            }
        },
        
        async handleHistory() {
            this.activeNav = 'history';
            this.showHistorySidebar = true;
            try {
                const response = await fetch(`/sessions/${this.userId}`);
                if (!response.ok) {
                    throw new Error('Failed to load sessions');
                }
                const data = await response.json();
                this.sessions = data.sessions;
            } catch (error) {
                console.error('Error loading sessions:', error);
                alert('加载历史记录失败：' + error.message);
            }
        },
        
        async loadSession(sessionId) {
            this.sessionId = sessionId;
            this.showHistorySidebar = false;
            this.activeNav = 'newChat';
            
            // 从后端加载历史消息
            try {
                const response = await fetch(`/sessions/${this.userId}/${sessionId}`);
                if (!response.ok) {
                    throw new Error('Failed to load session messages');
                }
                const data = await response.json();
                
                // 转换消息格式并显示
                this.messages = data.messages.map(msg => ({
                    text: msg.content,
                    isUser: msg.type === 'human',
                    ragTrace: msg.rag_trace || null,
                    isComplete: true
                }));
                
                this.$nextTick(() => {
                    this.scrollToBottom();
                });
            } catch (error) {
                console.error('Error loading session:', error);
                alert('加载会话失败：' + error.message);
                this.messages = [];
            }
        },

        async deleteSession(sessionId) {
            if (!confirm(`确定要删除会话 "${sessionId}" 吗？`)) {
                return;
            }

            try {
                const response = await fetch(`/sessions/${this.userId}/${sessionId}`, {
                    method: 'DELETE'
                });

                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(payload.detail || 'Delete failed');
                }

                this.sessions = this.sessions.filter(s => s.session_id !== sessionId);

                if (this.sessionId === sessionId) {
                    this.messages = [];
                    this.sessionId = 'session_' + Date.now();
                    this.activeNav = 'newChat';
                }

                if (payload.message) {
                    alert(payload.message);
                }
            } catch (error) {
                console.error('Error deleting session:', error);
                alert('删除会话失败：' + error.message);
            }
        },
        
        handleSettings() {
            this.activeNav = 'settings';
            this.showHistorySidebar = false;
            // 加载文档列表
            this.loadDocuments();
        },
        
        handleProfile() {
            this.activeNav = 'profile';
            this.showHistorySidebar = false;
            this.loadProfile();
        },

        async loadProfile() {
            try {
                const response = await fetch(`/profile/${this.userId}`);
                if (response.ok) {
                    const data = await response.json();
                    this.userProfile = data.profile;
                    this.$nextTick(() => this.initProfileFilters());
                }
            } catch (error) {
                console.error("加载个人档案失败", error);
            }
        },

        /** 初始化病历夹筛选（先医嘱项目，再报告时间） */
        initProfileFilters() {
            const recs = this.userProfile?.records;
            if (!recs?.length) {
                this.profileFilterOrder = '';
                this.profileFilterDate = '';
                return;
            }
            const orders = this.orderCategoryOptions;
            if (orders.length && !orders.includes(this.profileFilterOrder)) {
                this.profileFilterOrder = orders[0];
            }
            this.$nextTick(() => {
                const dates = this.reportDateOptionsForFilter;
                if (dates.length && !dates.includes(this.profileFilterDate)) {
                    this.profileFilterDate = dates[dates.length - 1];
                }
            });
        },

        onProfileOrderChange() {
            const dates = this.reportDateOptionsForFilter;
            this.profileFilterDate = dates.length ? dates[dates.length - 1] : '';
        },

        async deleteActiveMedicalRecord() {
            const snap = this.activeMedicalRecord;
            if (!snap) return;
            this.isDeletingMedicalRecord = true;
            try {
                await this.loadProfile();
                let rec = (this.userProfile?.records || []).find(
                    (r) => String(r.id || '') === String(snap.id || '')
                );
                if (!rec && !String(snap.id || '').trim()) {
                    rec = (this.userProfile?.records || []).find(
                        (r) =>
                            (r.order_category || '') === (snap.order_category || '') &&
                            (r.report_date || '') === (snap.report_date || '') &&
                            (r.source_filename || '') === (snap.source_filename || '')
                    );
                }
                if (!rec || !String(rec.id || '').trim()) {
                    alert('无法定位要删除的病历（可能缺少记录 id）。请刷新页面后再试。');
                    return;
                }
                const label = `${rec.order_category || '病历'} · ${rec.report_date || ''}`.trim();
                if (
                    !confirm(
                        `确定删除本条病历「${label || rec.id}」？\n该条的简要记忆将从病历夹与对话记忆中移除，且不可恢复。`
                    )
                ) {
                    return;
                }
                const response = await fetch(
                    `/profile/${this.userId}/records/${encodeURIComponent(String(rec.id).trim())}`,
                    { method: 'DELETE' }
                );
                if (!response.ok) {
                    let detail = '删除失败';
                    try {
                        const err = await response.json();
                        detail = err.detail || detail;
                    } catch (_) { /* ignore */ }
                    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
                }
                const data = await response.json();
                this.userProfile = data.profile;
                this.profileProgress = data.message || '已删除';
                this.$nextTick(() => this.initProfileFilters());
            } catch (e) {
                console.error('deleteActiveMedicalRecord', e);
                alert('删除失败：' + e.message);
            } finally {
                this.isDeletingMedicalRecord = false;
            }
        },

        followUpDateKey(day) {
            if (day == null) return '';
            const m = String(this.followUpCalendarMonth).padStart(2, '0');
            const ds = String(day).padStart(2, '0');
            return `${this.followUpCalendarYear}-${m}-${ds}`;
        },
        followUpEventsForDay(day) {
            if (day == null) return [];
            return this.followUpEventsByDate[this.followUpDateKey(day)] || [];
        },
        calCellDynamicClass(cell) {
            const has =
                cell &&
                cell.day &&
                this.followUpEventsForDay(cell.day).length > 0;
            return {
                'cal-cell--muted': !cell.day,
                'cal-cell--mark': cell.day && has,
                'cal-cell-interactive': !!has,
            };
        },
        _clampFollowUpBubblePos(clientX, clientY) {
            const w = 320;
            const h = 200;
            const pad = 12;
            let left = clientX + 8;
            let top = clientY + 8;
            if (left + w > window.innerWidth - pad) left = window.innerWidth - w - pad;
            if (left < pad) left = pad;
            if (top + h > window.innerHeight - pad) top = clientY - h - 12;
            if (top < pad) top = pad;
            this.followUpBubblePos = { left, top };
        },
        onFollowUpCalDayClick(cell, evt) {
            if (!cell || cell.day == null) return;
            const evs = this.followUpEventsForDay(cell.day);
            if (!evs.length) return;
            this.followUpBubbleDateKey = this.followUpDateKey(cell.day);
            this.followUpBubbleEvents = evs;
            this._clampFollowUpBubblePos(evt.clientX, evt.clientY);
            this.followUpBubbleVisible = true;
        },
        onFollowUpCalDayKeydown(cell, evt) {
            if (evt.key !== 'Enter') return;
            if (!cell || cell.day == null) return;
            const evs = this.followUpEventsForDay(cell.day);
            if (!evs.length) return;
            const el = evt.currentTarget;
            const rect = el.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            this.followUpBubbleDateKey = this.followUpDateKey(cell.day);
            this.followUpBubbleEvents = evs;
            this._clampFollowUpBubblePos(cx, cy);
            this.followUpBubbleVisible = true;
        },
        closeFollowUpBubble() {
            this.followUpBubbleVisible = false;
            this.followUpBubbleEvents = [];
            this.followUpBubbleDateKey = '';
        },
        prevFollowUpMonth() {
            this.closeFollowUpBubble();
            if (this.followUpCalendarMonth <= 1) {
                this.followUpCalendarMonth = 12;
                this.followUpCalendarYear--;
            } else {
                this.followUpCalendarMonth--;
            }
        },
        nextFollowUpMonth() {
            this.closeFollowUpBubble();
            if (this.followUpCalendarMonth >= 12) {
                this.followUpCalendarMonth = 1;
                this.followUpCalendarYear++;
            } else {
                this.followUpCalendarMonth++;
            }
        },
        handleDischargeSelect(event) {
            const files = event.target.files;
            if (files && files.length > 0) {
                this.selectedDischargeFile = files[0];
                this.dischargeProgress = '';
            }
        },
        triggerDischargeUpload() {
            if (this.$refs.dischargeInput) this.$refs.dischargeInput.click();
        },
        async uploadDischargeReport() {
            if (!this.selectedDischargeFile) {
                alert('请选择出院报告 PDF 或图片');
                return;
            }
            this.isUploadingDischarge = true;
            this.dischargeProgress = '正在解析出院报告与出院医嘱，请稍候…';
            try {
                const formData = new FormData();
                formData.append('file', this.selectedDischargeFile);
                const response = await fetch(`/profile/${this.userId}/discharge/upload`, {
                    method: 'POST',
                    body: formData,
                });
                if (!response.ok) {
                    const err = await response.json();
                    throw new Error(err.detail || '上传失败');
                }
                const data = await response.json();
                this.userProfile = data.profile;
                this.dischargeProgress = data.message || '解析成功';
                this.selectedDischargeFile = null;
                if (this.$refs.dischargeInput) this.$refs.dischargeInput.value = '';
            } catch (e) {
                console.error(e);
                this.dischargeProgress = '失败：' + e.message;
            } finally {
                this.isUploadingDischarge = false;
            }
        },
        async deleteDischargeReport(rep) {
            if (!rep || !rep.id) return;
            if (
                !confirm(
                    `确定删除出院报告「${rep.source_filename || rep.id}」？随访日历中的相关日期将一并移除。`
                )
            ) {
                return;
            }
            this.isDeletingDischargeReport = true;
            try {
                const response = await fetch(
                    `/profile/${this.userId}/discharge/${encodeURIComponent(rep.id)}`,
                    { method: 'DELETE' }
                );
                if (!response.ok) {
                    const err = await response.json();
                    throw new Error(err.detail || '删除失败');
                }
                const data = await response.json();
                this.userProfile = data.profile;
                this.dischargeProgress = data.message || '已删除';
            } catch (e) {
                alert('删除失败：' + e.message);
            } finally {
                this.isDeletingDischargeReport = false;
            }
        },

        /** 单条病历（MedicalRecordEntry）校对草稿 */
        normalizeRecordDraft(r) {
            const d = JSON.parse(JSON.stringify(r || {}));
            if (!Array.isArray(d.test_items)) d.test_items = [];
            d.test_items = d.test_items.map((t) => ({
                item_name: t.item_name ?? '',
                result: t.result ?? '',
                unit: t.unit ?? '',
                reference_range: t.reference_range ?? '',
                abnormal: t.abnormal ?? '',
                record_date: t.record_date ?? '',
            }));
            d.order_category = d.order_category ?? '';
            d.report_date = d.report_date ?? d.record_date ?? '';
            d.name = d.name ?? '';
            d.age = d.age ?? '';
            d.gender = d.gender ?? '';
            return d;
        },

        emptyTestItem() {
            return {
                item_name: '',
                result: '',
                unit: '',
                reference_range: '',
                abnormal: '',
                record_date: '',
            };
        },

        addProfileReviewTestRow() {
            if (!this.profileReviewDraft) return;
            this.profileReviewDraft.test_items.push(this.emptyTestItem());
        },

        removeProfileReviewTestRow(index) {
            if (!this.profileReviewDraft || !this.profileReviewDraft.test_items) return;
            this.profileReviewDraft.test_items.splice(index, 1);
        },

        closeProfileReviewModal() {
            this.showProfileReviewModal = false;
            this.profileReviewDraft = null;
            this.profileReviewRecordId = null;
        },

        async saveProfileReview() {
            if (!this.profileReviewDraft || this.profileReviewRecordId == null) return;
            this.isSavingProfileReview = true;
            try {
                const folder = JSON.parse(JSON.stringify(this.userProfile || {}));
                if (!Array.isArray(folder.records)) folder.records = [];
                const idx = folder.records.findIndex((r) => r.id === this.profileReviewRecordId);
                if (idx < 0) {
                    throw new Error('找不到对应病历记录');
                }
                const merged = this.normalizeRecordDraft(this.profileReviewDraft);
                merged.id = this.profileReviewRecordId;
                folder.records[idx] = merged;
                if (merged.name) folder.name = merged.name;
                if (merged.age) folder.age = merged.age;
                if (merged.gender) folder.gender = merged.gender;
                if (merged.report_date) folder.record_date = merged.report_date;
                folder.schema_version = 2;

                const response = await fetch(`/profile/${this.userId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ profile: folder }),
                });
                if (!response.ok) {
                    let detail = '保存失败';
                    try {
                        const err = await response.json();
                        detail = err.detail || detail;
                    } catch (_) { /* ignore */ }
                    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
                }
                const data = await response.json();
                this.userProfile = data.profile;
                this.profileProgress = data.message || '已保存校对后的档案';
                this.closeProfileReviewModal();
                this.$nextTick(() => this.initProfileFilters());
            } catch (error) {
                console.error('saveProfileReview', error);
                alert('保存失败：' + error.message);
            } finally {
                this.isSavingProfileReview = false;
            }
        },

        handleProfileSelect(event) {
            const files = event.target.files;
            if (files && files.length > 0) {
                this.selectedProfileFile = files[0];
                this.profileProgress = '';
            }
        },

        triggerProfileUpload() {
            if (this.$refs.profileInput) {
                this.$refs.profileInput.click();
            }
        },

        async uploadProfile() {
            if (!this.selectedProfileFile) {
                alert('请先选择病历文件');
                return;
            }
            
            this.isUploadingProfile = true;
            this.profileProgress = '正在使用多模态大模型解析病历资料，请稍候...';
            
            try {
                const formData = new FormData();
                formData.append('file', this.selectedProfileFile);
                formData.append('user_id', this.userId);
                formData.append('is_update', 'false');
                
                const response = await fetch('/profile/upload', {
                    method: 'POST',
                    body: formData
                });
                
                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Upload failed');
                }
                
                const data = await response.json();
                this.profileProgress = data.message;
                this.userProfile = data.profile;
                const recs = data.profile?.records || [];
                const last = recs[recs.length - 1];
                if (last) {
                    this.profileReviewRecordId = last.id;
                    this.profileReviewDraft = this.normalizeRecordDraft(last);
                    this.showProfileReviewModal = true;
                }

                // 清空选择
                this.selectedProfileFile = null;
                if (this.$refs.profileInput) {
                    this.$refs.profileInput.value = '';
                }
                this.$nextTick(() => this.initProfileFilters());
                
            } catch (error) {
                console.error('Error uploading profile:', error);
                this.profileProgress = '解析失败：' + error.message;
            } finally {
                this.isUploadingProfile = false;
            }
        },

        sendProfileFollowUp(question) {
            this.activeNav = 'newChat';
            this.userInput = question;
            this.$nextTick(() => {
                this.handleSend();
            });
        },

        renderChart() {
            if (!this.selectedChartIndicator || !this.userProfile?.records?.length) return;

            const recs = this.userProfile.records || [];
            const pool = this.profileFilterOrder
                ? recs.filter((r) => (r.order_category || '') === this.profileFilterOrder)
                : recs;
            const dataPoints = [];
            for (const r of pool) {
                for (const item of r.test_items || []) {
                    if (item.item_name === this.selectedChartIndicator) {
                        dataPoints.push({
                            ...item,
                            _labelDate: item.record_date || r.report_date || '',
                        });
                    }
                }
            }
            dataPoints.sort(
                (a, b) => new Date(a._labelDate || 0) - new Date(b._labelDate || 0)
            );

            const labels = dataPoints.map((item) => item._labelDate || '未知时间');
            const values = dataPoints.map((item) => parseFloat(item.result) || 0);

            this.$nextTick(() => {
                const ctx = document.getElementById('indicatorChart');
                if (!ctx) return;
                
                if (this.chartInstance) {
                    this.chartInstance.destroy();
                }
                
                this.chartInstance = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: this.selectedChartIndicator,
                            data: values,
                            borderColor: '#2ecc71', // 绿色折线
                            backgroundColor: 'rgba(46, 204, 113, 0.1)',
                            borderWidth: 2,
                            pointBackgroundColor: '#27ae60', // 较深的绿点
                            pointBorderColor: '#fff',
                            pointBorderWidth: 2,
                            pointRadius: 5,
                            pointHoverRadius: 7,
                            fill: true,
                            tension: 0.3
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                display: false // 隐藏顶部的冗余图例（Data-ink ratio）
                            },
                            tooltip: {
                                backgroundColor: 'rgba(255,255,255,0.9)',
                                titleColor: '#333',
                                bodyColor: '#666',
                                borderColor: '#eee',
                                borderWidth: 1,
                                padding: 10,
                                displayColors: false
                            }
                        },
                        scales: {
                            x: {
                                grid: {
                                    display: false, // 隐藏 X 轴网格线
                                    drawBorder: false
                                },
                                ticks: {
                                    font: { family: "'Nunito', sans-serif" },
                                    color: '#888'
                                }
                            },
                            y: {
                                beginAtZero: false,
                                grid: {
                                    display: false, // 隐藏 Y 轴网格线
                                    drawBorder: false
                                },
                                ticks: {
                                    font: { family: "'Nunito', sans-serif" },
                                    color: '#888'
                                }
                            }
                        }
                    }
                });
            });
        },

        async loadDocuments() {
            this.documentsLoading = true;
            try {
                const response = await fetch('/documents');
                if (!response.ok) {
                    throw new Error('Failed to load documents');
                }
                const data = await response.json();
                this.documents = data.documents;
            } catch (error) {
                console.error('Error loading documents:', error);
                alert('加载文档列表失败：' + error.message);
            } finally {
                this.documentsLoading = false;
            }
        },
        
        handleFileSelect(event) {
            const files = event.target.files;
            if (files && files.length > 0) {
                this.selectedFile = files[0];
                this.uploadProgress = '';
            }
        },
        
        async uploadDocument() {
            if (!this.selectedFile) {
                alert('请先选择文件');
                return;
            }
            
            this.isUploading = true;
            this.uploadProgress = '正在上传...';
            
            try {
                const formData = new FormData();
                formData.append('file', this.selectedFile);
                
                const response = await fetch('/documents/upload', {
                    method: 'POST',
                    body: formData
                });
                
                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Upload failed');
                }
                
                const data = await response.json();
                this.uploadProgress = data.message;
                
                // 清空选择
                this.selectedFile = null;
                if (this.$refs.fileInput) {
                    this.$refs.fileInput.value = '';
                }
                
                // 刷新文档列表
                await this.loadDocuments();
                
                // 3秒后清除提示
                setTimeout(() => {
                    this.uploadProgress = '';
                }, 3000);
                
            } catch (error) {
                console.error('Error uploading document:', error);
                this.uploadProgress = '上传失败：' + error.message;
            } finally {
                this.isUploading = false;
            }
        },
        
        async deleteDocument(filename) {
            if (!confirm(`确定要删除文档 "${filename}" 吗？这将同时删除 Milvus 中的所有相关向量。`)) {
                return;
            }
            
            try {
                const response = await fetch(`/documents/${encodeURIComponent(filename)}`, {
                    method: 'DELETE'
                });
                
                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Delete failed');
                }
                
                const data = await response.json();
                alert(data.message);
                
                // 刷新文档列表
                await this.loadDocuments();
                
            } catch (error) {
                console.error('Error deleting document:', error);
                alert('删除文档失败：' + error.message);
            }
        },
        
        getFileIcon(fileType) {
            if (fileType === 'PDF') {
                return 'fas fa-file-pdf';
            } else if (fileType === 'Word') {
                return 'fas fa-file-word';
            } else if (fileType === 'Excel') {
                return 'fas fa-file-excel';
            }
            return 'fas fa-file';
        }
    },
        watch: {
        showChartModal(newVal) {
            if (newVal) {
                if (!this.selectedChartIndicator && this.uniqueTestItemNames.length > 0) {
                    this.selectedChartIndicator = this.uniqueTestItemNames[0];
                }
                this.$nextTick(() => {
                    this.renderChart();
                });
            }
        },
        messages: {
            handler() {
                this.$nextTick(() => {
                    this.scrollToBottom();
                });
            },
            deep: true
        }
    }
}).mount('#app');
