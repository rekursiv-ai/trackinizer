// RECHARTS: rich interactive charts (Area, Bar, Line, Radar, Radial, Pie).
// Data comes from the answer's own figures; the catalog mounts Recharts in the design's ink.
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Line,
  LineChart,
  Pie,
  PieChart,
  Radar,
  RadarChart,
  RadialBar,
  RadialBarChart,
  CartesianGrid,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Cell,
} from "recharts";

export type RechartsData = readonly (Readonly<Record<string, unknown>>)[];
export type RechartsType = "area" | "bar" | "line" | "radar" | "radial" | "pie";

export function RechartsView({ type, data, label }: {
  readonly type: RechartsType;
  readonly data: RechartsData;
  readonly label?: string;
}) {
  const common = (
    <>
      <CartesianGrid strokeDasharray="3 3" opacity={0.1} />
      <XAxis dataKey="name" />
      <YAxis />
      <Tooltip />
    </>
  );

  return (
    <div className="ui-recharts" role="img" aria-label={label ?? "recharts chart"}>
      <ResponsiveContainer width="100%" height="100%">
        {type === "area" && (
          <AreaChart data={data as any[]}><>{common}</><Area type="monotone" dataKey="value" stroke="currentColor" fill="currentColor" fillOpacity={0.2} /></AreaChart>
        )}
        {type === "bar" && (
          <BarChart data={data as any[]}><>{common}</><Bar dataKey="value" fill="currentColor" /></BarChart>
        )}
        {type === "line" && (
          <LineChart data={data as any[]}><>{common}</><Line type="monotone" dataKey="value" stroke="currentColor" /></LineChart>
        )}
        {type === "radar" && (
          <RadarChart data={data as any[]}>
            <PolarGrid />
            <PolarAngleAxis dataKey="name" />
            <PolarRadiusAxis />
            <Radar dataKey="value" stroke="currentColor" fill="currentColor" fillOpacity={0.4} />
            <Tooltip />
          </RadarChart>
        )}
        {type === "radial" && (
          <RadialBarChart data={data as any[]} innerRadius="20%" outerRadius="90%">
            <PolarAngleAxis dataKey="name" />
            <RadialBar dataKey="value" />
            <Tooltip />
          </RadialBarChart>
        )}
        {type === "pie" && (
          <PieChart>
            <Pie data={data as any[]} dataKey="value" nameKey="name" label>
              {data.map((_, i) => <Cell key={i} fill="currentColor" opacity={1 - i * 0.08} />)}
            </Pie>
            <Tooltip />
          </PieChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
