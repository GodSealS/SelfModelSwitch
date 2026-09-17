# 07 视频流水线、检查点和结果质量

## 1. 统一时间与数据约束

所有时间是相对原片首个有效视频PTS的整数毫秒，区间半开；内部保留time_base分数和原始PTS供重编码。
变帧率按PTS取帧，禁止用frame_index/fps替代实际时间。probe的audio.wav已对齐视频零点：原音轨负偏移裁切，正偏移补静音。
audio_offset_ms仅记录原容器偏移供审计，ASR不得再次加偏移。末尾补静音/裁切至video duration，WAV采样数按毫秒*16计算。
元数据记录duration_ms、video_time_base、audio_offset_ms、width、height、rotation、frame_count（未知可null）。
所有业务JSON顶层有schema_version=1、job_id、input_sha256、candidate_sha256、stage_id、artifacts。
所有数组按时间/ID稳定排序；ID在一个job内唯一。输入duration之外的区间是验证错误，不能悄悄clip不可信后端结果。
后端局部时间合法后，才按offset转换全局并裁到合法音频chunk边界。

## 2. 固定阶段顺序

| stage_id | 后端/单位 | 输入 | 成功产物 |
|---|---|---|---|
| probe | media/整片 | input登记文件 | media.json、16k mono PCM16 audio.wav |
| shots | media/整片 | 原片、media.json | shots.json、每镜头首中尾JPEG |
| asr | MOSS/音频块 | audio.wav | transcript.json、SRT、ASS |
| face_tracks | detection worker/镜头 | 原片、shots | tracks.jsonl、cropped faces、待复核区间 |
| face_identity | embedding worker/track代表脸 | crops、tracks | persons.json、track/person映射 |
| depth | depth worker/镜头中帧 | shots | 单通道depth PNG、预览PNG、depth-index.json |
| vision | llama VL/镜头 | 原始首中尾帧 | shot-descriptions.json |
| mask | media/整片流式 | 原片、逐帧tracks | movie-masked.mp4、render-index.json |
| report | 外部adapter/按预算分组再汇总 | 上述结构化结果 | report.json、report.md、evidence-index.json |

只有full scope包含report。report不申请本地模型permit，前一mask阶段必须已确认STOPPED；
它仍是runner唯一活动stage，外部网络调用的超时/幂等见第7节。
probe/shots/mask都申请model_id=null的media permit；worker实例受同样预算和停止规则约束。
每阶段可有多个unit，但一次一个；同stage复用模型直到全部unit完成，之后关闭permit。
unit清单在stage开始前一次事务提交，规划器实现hash进candidate，恢复不能重规划成不同切片。

## 3. 各产物的接口和算法

### 3.1 分镜

Shot精确字段：shot_id（整数>=1）、start_ms、end_ms、frames（FrameRef恰好3项）。
FrameRef字段：artifact_id、pts_ms、role(first/middle/last)、width、height。
镜头覆盖`[0,duration_ms)`无重叠无空洞；首帧在start或其后最近PTS，中帧最接近区间中点（相同距离取较早），
尾帧为end前最后一帧。短镜头允许三个引用指向同一帧。尺寸保持宽高比，最长边<=1280，不拉伸到固定16:9。
首版PySceneDetect content threshold=27.0，最短镜头15帧；参数和库版本进candidate，不使用附件中的猜测CLI拼接。

### 3.2 转写与说话人

chunk最大1500000ms，步长1498000ms，相邻重叠2000ms；末块可短于上限；chunk边界清单先落库。
Segment字段：segment_id、chunk_id、local_speaker、speaker_id、person_id(null允许)、start_ms、end_ms、text、
association（unknown/manual）、evidence_ids（数组）。speaker_id固定`<chunk_id>_<local_speaker>`，禁止跨块直接合并S01。
局部speaker必须匹配`S[0-9]{2,}`，局部时间必须在该chunk范围，文本非空。
全局时间=chunk_start+local_time（audio.wav已对齐，不再加audio_offset）。重叠区分界为两块交叠区中点；保留segment中点落在该chunk负责区间的segment，
中点恰在边界归后一块；同一块内按start/end/segment_id排序，不因文本相同删除不同时间的台词。
该确定性算法不能保证模型在断句边界无丢词/重复，Q02必须单独包含跨边界句子。
跨块说话人合并和Sxx→人物只接受可选人工identity_map，字段(source_speaker_id,target_person_id,evidence_ids,reviewer_id)。
无映射时保留unknown；不以“同时间画面有人”自动判定其在说话。首版不承诺自动全片声纹聚类。
SRT/ASS使用全片时间且显示匿名speaker_id；不得输出重置为0的后半段字幕。

### 3.3 检测、跟踪、身份与出现区间

TrackRow字段：frame_pts_ms、shot_id、track_id、bbox_xyxy（4个整数原片像素坐标）、confidence（0..1）、
source(detection/tracking)、needs_review（bool）。边界0<=x1<x2<=width、0<=y1<y2<=height，缩放/rotation映射后再写。
每镜头首帧检测，其后每5帧检测；中间帧采用manifest固定tracker，发生跟踪丢失/新脸候选时立即重检测。
首版tracker选OpenCV固定版本CSRT，每track独立；多脸关联用IoU最大匹配，IoU>=0.3，平分按track_id，跨镜头不延续track。
检测阈值0.6；tracker越界/空框/与重检测IoU<0.3的间隙标记needs_review，并对该间隙逐帧重检测。
仍未解决的间隙记录review interval；mask阶段对此区间采用整帧模糊兜底，不能直接输出原画面。
未检测到的人脸仍可能漏检，因此真正覆盖率由Q04标注集评估，不能以“无review interval”推断零漏码。

每track最多取5张有效crop（等时间分位数、相同PTS去重），embedding L2归一化。
track向量为样本均值再L2归一化；无有效embedding则独立unknown person。所有track按首PTS/track_id排序。
首版使用complete-link聚类：余弦距离<=0.35才允许两簇合并，选择最大簇间距离最小的合并，平分取ID字典序；
一旦同帧存在两个不同track，禁止把它们合并。阈值/算法固定进参数hash，不测后调整而沿用报告。
person_id按最终簇最早track排序分配P0001...；名字默认null，只有人工映射赋名。
Person字段：person_id、name(null)、track_ids、spans（Span列表）、face_samples（artifact IDs）、identity_source(cluster/manual)。
spans由该person的有效逐帧观测覆盖区间并集构成，相邻间隔<=100ms合并；跨镜头不以整个镜头边界补齐。
每帧覆盖至下一视频PTS，末帧覆盖至镜头end；needs_review行不作为“已确认出现”证据。出场总时长按区间并集，不重复加重叠。

### 3.4 深度

每shot中帧一张相对深度，不宣称米制/跨镜头绝对尺度一致。输入最长边<=518，按模型要求补齐倍数，记录padding和resize映射。
原始浮点输出必须有限；按该图min/max线性映射uint16 PNG，若max=min则全零并记录constant=true，不能除零。
DepthIndex字段：shot_id、frame_artifact_id、depth_artifact_id、preview_artifact_id、width、height、
raw_min、raw_max、constant、semantics（固定relative_inverse_depth）、resize_transform。
预览图不作测量输入；评价使用原始深度或可逆min/max还原值。

### 3.5 VLM

ShotDescription字段：shot_id、frame_ids、location（string|null）、persons_visible（person_id数组）、action（string|null）、
emotion（string|null）、on_screen_text（string|null）、shot_type（string|null）、evidence_frame_ids、uncertain_fields（字段名数组）。
runner同时在文本输入提供该镜头每帧(person_id,bbox_xyxy,frame_id)对照，从已验证tracks/persons生成；
模型必须输出JSON并验证；person_id只能来自本镜头提供的对照，不能仅凭图像猜匿名ID或姓名；无法确认返回空数组+uncertain。
失败不自动重试；记录unit failed，显式resume可重跑。text只是有证据的描述，不将情绪/角色关系推断当身份事实。
每镜头最多3帧，max_output_tokens与context按candidate envelope限制；超出输入/输出限制明确失败。

### 3.6 打码

按每个原片PTS的TrackRow在原分辨率渲染，框向外扩20%（每侧原宽/高10%，向外取整后裁边）。
马赛克缩小到16x16再nearest放大，目标框小于16像素则用min(width,16)/min(height,16)。
needs_review未消除区间使用整帧高斯模糊（sigma=30、kernel按最接近奇数的6*sigma+1即181），产物metadata明确列区间。
首版输出H.264 MP4、原尺寸/方向、原PTS时间线；音频可兼容时复制，不兼容时AAC并记录转码参数。
输出视频必须完整解码，无丢帧/时间倒退；A/V起始与结束偏差各<=80ms，video duration偏差<=一帧最大时长+20ms。
不允许从三张缩略图还原电影，不允许将检测crop坐标直接用于原片。

## 4. 持久化与幂等

SQLite：WAL、foreign_keys=ON、synchronous=FULL；单写连接，DB migration版本记录。
表与唯一键：jobs(job_id, idempotency_key UNIQUE, request_hash, state, candidate_hash, input_hash, timestamps)；
stage_attempts(job_id,stage_id,attempt UNIQUE组合,state)；units(job_id,stage_id,unit_id UNIQUE组合,input_hash,param_hash)；
unit_attempts(job_id,stage_id,unit_id,attempt UNIQUE组合,state,execution_id)；artifacts(artifact_id PRIMARY KEY, attempt FK,hash,path,size)；
external_calls(call_id PRIMARY KEY,idempotency_key UNIQUE,provider_request_id,state,response_hash)。
每次状态变更CAS校验旧state/attempt；事务内不能执行模型/网络IO。
unit缓存键=SHA256规范JSON(job input hash,stage,unit切片定义,parameters hash,model assets hash,implementation hash)。
媒体阶段model_assets_hash为规范JSON空数组的SHA256，不能填全零占位。

产物提交严格顺序：写attempt临时目录 → fsync每文件 → 验证内容/尺寸/hash → rename至不可变最终目录 → fsync父目录 →
SQLite事务插入Artifact并把unit attempt改SUCCEEDED。CAS失败则文件为孤儿，不能覆盖其它attempt产物。
崩溃恢复只复用DB中已提交且重新hash匹配的产物；未登记文件隔离到job/orphans，不直接认定成功。
已提交产物损坏将对应unit及所有下游stage置INVALIDATED，job PAUSED；显式resume从最早失效stage重跑。
失败本地unit最多由用户显式resume重跑，不自动无限重试；已成功前置unit不重做。

## 5. 质量数据集

验收前冻结dataset manifest：每段原片hash、标签hash、split、语言、场景标签、标注员、裁切PTS、许可来源。
不要求把私人影片提交Git。至少12段、总长>=30min，覆盖剪切/淡入淡出、快运动、侧脸/遮挡、多脸、暗光、画外音、
旁白、重叠说话、字幕以及跨音频切片边界；每个标签至少2段，可重叠覆盖。两名标注员独立标注并裁决分歧。
完整长片120min±5min、1080p用于P01；必须不等同于短片拼接循环。性能和质量不能只在同一小样例上验收。
原始标注schema纳入dataset hash；labels在evaluation只读挂载，不提供给被测模型。

## 6. 质量门槛（quality policy v1）

这些是本项目初版通过标准，不是对指定模型的效果保证。指标版本和参数进入quality_policy_sha256。

| ID | 指标、算法 | 必须满足 |
|---|---|---|
| Q01 | 预测/标注镜头边界在±200ms内一对一最大匹配；不计影片首尾，P/R/F1 | micro F1>=0.90，每段F1>=0.75；全片镜头区间结构100%合法 |
| Q02 | 文本Unicode NFKC、小写、移除Unicode标点和空白；字符Levenshtein总错误/参考字符数。DER按10ms格点、250ms边界collar、忽略重叠语音，对每chunk独立最优speaker匹配 | micro CER<=0.20；DER<=0.25；匹配正确句的起止绝对误差P95<=500ms；跨块timestamp回退=0，误合并不同chunk S01=0 |
| Q03 | face box IoU>=0.5一对一匹配；出现区间按10ms格点评价；身份聚类pairwise precision/recall（正负对按全label集合枚举） | face recall>=0.95；出现区间micro F1>=0.90；聚类precision>=0.98、recall>=0.85 |
| Q04 | 标注短片所有帧全部face框逐一检查；有效mask覆盖face框面积比例>=0.99才算covered。render-index与实际输出像素变化交叉验证 | uncovered faces=0；每个标注脸覆盖>=0.99；A/V/PTS满足3.6；未知mask证据失败 |
| Q05 | 固定标注像素点对远近顺序，在还原深度上评价；并检查尺寸/有限值/对应shot | 每图>=20有效点对，>=100镜头；正确比例>=0.90；结构错误=0 |
| Q06 | 人工核对每个输出原子可验证事实；正确事实数/全部可验证事实，未知不计正确；参考主要动作的覆盖率单独评价 | factual precision>=0.95，主要动作覆盖>=0.85；引用帧不存在=0，凭空person ID=0 |

无正样本/分母为0不是通过，标记数据集不合格。micro先累加分子分母再计算，不平均各片百分比。
P95使用nearest-rank：排序后第ceil(0.95*n)项。DER=(miss+false_alarm+speaker_confusion)/reference_speaker_time；
按chunk评分不证明全片同一说话人ID连续，报告必须注明此限制。Q03须至少两个身份且存在同身份跨镜头正对。
Q04短片必须逐帧标注；长片另按每60s取连续2s以及所有needs_review区间人工审看，覆盖范围写报告，不能宣称全片逐帧人工验证。
整帧兜底比例=兜底帧数/总帧数，必须<=0.01；超过仍可生成草稿视频，但不能通过Q04产品验收。
Q05表示相对深度排序能力，不是metric accuracy。Q06主观情绪标为uncertain，不计入主要动作覆盖。

Q02时间误差的匹配规则：参考以标注的utterance_id为单位，预测以segment_id为单位；候选边要求中心时间差<=2000ms、
正规化文本相似度`1-Levenshtein/max(len(ref),len(pred))>=0.8`。按相似度降序、中心差升序、utterance_id、segment_id排序，
贪心选择双方尚未匹配的边。对匹配边收集起/止两个误差一起算P95；参考utterance匹配率必须>=0.80，否则Q02失败。
CER仍对整片拼接文本计算，不仅计算匹配句。空字符串候选边不匹配。DER按已冻结10ms格点及chunk切分计算。
Q06参考标注预先列每shot的action_id及一个主谓宾动作事实；不可合并多个动作成一个标注。
两名审核者各自把预测陈述拆成原子事实并给出支持/不支持/不可验证标签以及对应action_id，分歧由第三人裁决；
审核表含原始输出hash、参考标注hash和裁决记录，不修改预封存参考答案。
factual precision=支持的可验证预测事实数/全部可验证预测事实数；重复同一事实只计一次（按裁决后的事实ID去重）。
主要动作覆盖=至少一个受支持预测命中的不同action_id数/参考action_id总数。不可验证不计正确，也不计可验证分母；
可验证预测为0或参考动作为0判不合格，禁止全写unknown取得满分。

## 7. 外部报告

仅发送台词、镜头描述、人物/时间区间及证据ID，不默认上传原视频/脸图。provider/endpoint/model/prompt版本均锁入adapter配置。
按adapter声明的tokenizer计数，单请求输入<=provider context的60%，预留输出<=20%；超限按shot边界切批。
每批先抽取关系事实，再汇总结构化关系；最终Markdown由本地模板渲染，不解析自由Markdown作为事实源。
ReportFact字段：subject_person_id、predicate、object_person_id(null允许)、span、evidence_ids（非空）、confidence、uncertain。
Evidence必须引用已有segment/shot/track，时间相交，未知人物保持匿名。外部文本中的新姓名不自动回写persons.json。
每个批次call_id及请求hash先落DB再发送；provider支持幂等键则使用该键，仍不自动重发不确定结果。
超时/runner重启时无法判定服务是否已执行→EXTERNAL_UNKNOWN，job PAUSED；resume显式allow_external_retry=true才继续。
预算上限（token/费用）在adapter配置为必填正值，触顶暂停；凭据从环境读取，不写产物或报告。
E01要求事实引用存在率100%、人工事实支持率>=0.95、无依据的确定性人物关系=0；E02/E03验证失败/重试及凭据边界。
