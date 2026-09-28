// SensorBridge - LibreHardwareMonitor 的轻量 stdout 桥接器
// 编译: csc.exe /platform:x64 /target:exe /out:SensorBridge.exe /r:LibreHardwareMonitorLib.dll SensorBridge.cs
// 用法:
//   SensorBridge.exe --interval 1000         持续输出遥测(默认)
//   SensorBridge.exe --probe                 打印一次全部传感器(人类可读)后退出
//   SensorBridge.exe --header-only           只输出头部后退出
//
// 输出协议(每行一条, 以 '|' 分隔抬头, 主体为紧凑 JSON):
//   H|<json>   头部: [{"i":0,"n":"AMD Ryzen 7 5700X","t":"Cpu","p":"","s":[{"i":0,"n":"CPU Total","t":"Load"},...]}]
//   D|<json>   数据: [[h,s,v],...]  v 为 null 表示无读数
//   E|<text>   错误/日志(写到 stderr, 不污染 stdout 协议)
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;
using LibreHardwareMonitor.Hardware;

namespace DshSensorBridge
{
    internal sealed class UpdateVisitor : IVisitor
    {
        public void VisitComputer(IComputer computer)
        {
            computer.Traverse(this);
        }

        public void VisitHardware(IHardware hardware)
        {
            hardware.Update();
            foreach (IHardware sub in hardware.SubHardware)
            {
                sub.Accept(this);
            }
        }

        public void VisitSensor(ISensor sensor) { }

        public void VisitParameter(IParameter parameter) { }
    }

    internal sealed class FlatSensor
    {
        public int HardwareIndex;
        public int SensorIndex;
        public string HardwareName;
        public string HardwareType;
        public string ParentName;
        public string Name;
        public string Type;
        public string Unit;
        public ISensor Sensor;
    }

    internal static class Program
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        private static string libDir = null;

        private static int Main(string[] args)
        {
            AppDomain.CurrentDomain.AssemblyResolve += ResolveFromLibDir;
            string exeDir = Path.GetDirectoryName(typeof(Program).Assembly.Location);
            string candidate = Path.GetFullPath(Path.Combine(exeDir, @"..\lhm"));
            if (Directory.Exists(candidate)) libDir = candidate;
            try
            {
                return Run(args);
            }
            catch (Exception ex)
            {
                try
                {
                    Console.Error.WriteLine("E|fatal: " + ex.GetType().FullName + ": " + ex.Message);
                    Console.Error.WriteLine("E|stack: " + ex.StackTrace);
                }
                catch (Exception) { }
                return 3;
            }
        }

        private static System.Reflection.Assembly ResolveFromLibDir(object sender, ResolveEventArgs e)
        {
            if (libDir == null) return null;
            try
            {
                string simple = new System.Reflection.AssemblyName(e.Name).Name;
                string path = Path.Combine(libDir, simple + ".dll");
                if (File.Exists(path)) return System.Reflection.Assembly.LoadFrom(path);
            }
            catch (Exception) { }
            return null;
        }

        private static int Run(string[] args)
        {
            int interval = 1000;
            bool probe = false;
            bool headerOnly = false;
            bool verbose = false;

            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i];
                if (a == "--interval" && i + 1 < args.Length)
                {
                    int.TryParse(args[++i], NumberStyles.Integer, Inv, out interval);
                }
                else if (a.StartsWith("--interval=", StringComparison.Ordinal))
                {
                    int.TryParse(a.Substring(11), NumberStyles.Integer, Inv, out interval);
                }
                else if (a == "--probe") probe = true;
                else if (a == "--header-only") headerOnly = true;
                else if (a == "--verbose") verbose = true;
            }

            if (interval < 100) interval = 100;

            try
            {
                Console.OutputEncoding = new UTF8Encoding(false);
            }
            catch (Exception)
            {
                // 某些宿主不支持设置编码, 忽略
            }

            TextWriter stdout = Console.Out;
            TextWriter stderr = Console.Error;

            Computer computer = new Computer();
            computer.IsCpuEnabled = true;
            computer.IsGpuEnabled = true;
            computer.IsMemoryEnabled = true;
            computer.IsMotherboardEnabled = true;
            computer.IsControllerEnabled = true;
            computer.IsNetworkEnabled = true;
            computer.IsStorageEnabled = true;
            computer.IsPsuEnabled = true;
            computer.IsBatteryEnabled = false;
            computer.IsPowerMonitorEnabled = true;

            try
            {
                computer.Open();
            }
            catch (Exception ex)
            {
                stderr.WriteLine("E|computer.Open failed: " + ex.GetType().Name + ": " + ex.Message);
                return 2;
            }

            UpdateVisitor visitor = new UpdateVisitor();

            // 首次遍历, 建立硬件/传感器清单
            try
            {
                computer.Accept(visitor);
            }
            catch (Exception ex)
            {
                stderr.WriteLine("E|first traverse failed: " + ex.Message);
            }

            List<FlatSensor> flat = new List<FlatSensor>();
            StringBuilder header = new StringBuilder();
            header.Append("H|[");

            int hwCount = computer.Hardware.Count;
            for (int h = 0; h < hwCount; h++)
            {
                IHardware hw = computer.Hardware[h];
                if (h > 0) header.Append(',');
                header.Append("{\"i\":").Append(h).Append(',');
                header.Append("\"n\":").Append(Js(hw.Name)).Append(',');
                header.Append("\"t\":").Append(Js(hw.HardwareType.ToString())).Append(',');
                header.Append("\"p\":").Append(Js(hw.Parent == null ? "" : hw.Parent.Name)).Append(',');
                header.Append("\"s\":[");

                int sIdx = 0;
                sIdx = AppendSensors(header, flat, hw, h, sIdx, true);

                for (int k = 0; k < hw.SubHardware.Length; k++)
                {
                    IHardware sub = hw.SubHardware[k];
                    sIdx = AppendSensors(header, flat, sub, h, sIdx, false);
                }

                header.Append("]}");
            }
            header.Append(']');

            if (probe)
            {
                stdout.WriteLine("== hardware count: " + hwCount + " ==");
                for (int i = 0; i < flat.Count; i++)
                {
                    FlatSensor f = flat[i];
                    float? v = null;
                    try { v = f.Sensor.Value; }
                    catch (Exception) { }
                    stdout.WriteLine(string.Format(Inv, "{0,-10} | {1,-26} | {2,-28} | {3,-12} | {4}",
                        f.HardwareType, Shorten(f.HardwareName, 26), Shorten(f.Name, 28), f.Type,
                        v.HasValue ? v.Value.ToString("0.###", Inv) : "-"));
                }
                stdout.Flush();
                try { computer.Close(); }
                catch (Exception) { }
                return 0;
            }

            stdout.WriteLine(header.ToString());
            stdout.Flush();

            if (headerOnly)
            {
                try { computer.Close(); }
                catch (Exception) { }
                return 0;
            }

            StringBuilder buf = new StringBuilder(8192);
            bool running = true;
            Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e)
            {
                e.Cancel = true;
                running = false;
            };

            while (running)
            {
                int started = Environment.TickCount;
                try
                {
                    computer.Accept(visitor);
                }
                catch (Exception ex)
                {
                    stderr.WriteLine("E|update: " + ex.Message);
                }

                buf.Length = 0;
                buf.Append("D|[");
                bool first = true;
                for (int i = 0; i < flat.Count; i++)
                {
                    FlatSensor f = flat[i];
                    float? v = null;
                    try { v = f.Sensor.Value; }
                    catch (Exception) { }
                    if (!v.HasValue) continue;
                    float val = v.Value;
                    if (float.IsNaN(val) || float.IsInfinity(val)) continue;
                    if (!first) buf.Append(',');
                    first = false;
                    buf.Append('[').Append(f.HardwareIndex).Append(',').Append(f.SensorIndex).Append(',');
                    buf.Append(Math.Round(val, 3).ToString(Inv));
                    buf.Append(']');
                }
                buf.Append(']');

                stdout.WriteLine(buf.ToString());
                stdout.Flush();

                int elapsed = unchecked(Environment.TickCount - started);
                int sleep = interval - elapsed;
                if (sleep < 20) sleep = 20;
                int waited = 0;
                while (running && waited < sleep)
                {
                    int step = sleep - waited;
                    if (step > 100) step = 100;
                    Thread.Sleep(step);
                    waited += step;
                }
            }

            try { computer.Close(); }
            catch (Exception) { }
            return 0;
        }

        private static int AppendSensors(StringBuilder sb, List<FlatSensor> flat, IHardware hw,
            int hardwareIndex, int startIndex, bool emitName)
        {
            int idx = startIndex;
            IList<ISensor> sensors = hw.Sensors;
            for (int i = 0; i < sensors.Count; i++)
            {
                ISensor s = sensors[i];
                FlatSensor f = new FlatSensor();
                f.HardwareIndex = hardwareIndex;
                f.SensorIndex = idx;
                f.HardwareName = hw.Name;
                f.HardwareType = hw.HardwareType.ToString();
                f.ParentName = hw.Parent == null ? "" : hw.Parent.Name;
                f.Name = s.Name;
                f.Type = s.SensorType.ToString();
                f.Unit = UnitFor(s.SensorType);
                f.Sensor = s;
                flat.Add(f);

                if (idx > 0) sb.Append(',');
                sb.Append("{\"i\":").Append(idx).Append(',');
                sb.Append("\"n\":").Append(Js(s.Name)).Append(',');
                sb.Append("\"t\":").Append(Js(s.SensorType.ToString())).Append(',');
                sb.Append("\"u\":").Append(Js(f.Unit)).Append('}');
                idx++;
            }
            return idx;
        }

        private static string UnitFor(SensorType t)
        {
            switch (t)
            {
                case SensorType.Voltage: return "V";
                case SensorType.Current: return "A";
                case SensorType.Power: return "W";
                case SensorType.Clock: return "MHz";
                case SensorType.Temperature: return "C";
                case SensorType.Load: return "%";
                case SensorType.Frequency: return "Hz";
                case SensorType.Fan: return "RPM";
                case SensorType.Flow: return "L/h";
                case SensorType.Control: return "%";
                case SensorType.Level: return "%";
                case SensorType.Factor: return "1";
                case SensorType.Data: return "GB";
                case SensorType.SmallData: return "MB";
                case SensorType.Throughput: return "B/s";
                case SensorType.TimeSpan: return "s";
                case SensorType.Energy: return "mWh";
                case SensorType.Humidity: return "%";
                default: return "";
            }
        }

        private static string Shorten(string s, int max)
        {
            if (s == null) return "";
            if (s.Length <= max) return s;
            return s.Substring(0, max - 1) + "…";
        }

        private static string Js(string s)
        {
            if (s == null) return "\"\"";
            StringBuilder sb = new StringBuilder(s.Length + 8);
            sb.Append('"');
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    default:
                        if (c < ' ')
                        {
                            sb.Append("\\u").Append(((int)c).ToString("x4", Inv));
                        }
                        else
                        {
                            sb.Append(c);
                        }
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }
    }
}
